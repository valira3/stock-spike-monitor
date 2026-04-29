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
    above Entry-1 HWM. Section I open.

    v5.13.7 N1: Entry-2 share count == Entry-1 share count (50/50 split
    by share count, per spec L-P3-S6). Pre-v5.13.7 this was a dollar-
    notional top-up that drifted with price.
    """
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
    # v5.13.7 N1: E2 share count == E1 share count (10).
    expected_e2 = 10
    assert pos["v5104_entry2_shares"] == expected_e2
    assert pos["shares"] == 10 + expected_e2
    # Avg entry weighted by shares.
    expected_avg = (100 * 10 + 110 * expected_e2) / (10 + expected_e2)
    assert abs(pos["entry_price"] - expected_avg) < 1e-6


def test_paper_shares_for_uses_entry_1_size_pct(tg, monkeypatch):
    """v5.13.2 Track A: paper_shares_for must apply ENTRY_1_SIZE_PCT
    so Entry-1 only consumes 50% of PAPER_DOLLARS_PER_ENTRY notional.
    """
    from broker.orders import paper_shares_for
    from eye_of_tiger import ENTRY_1_SIZE_PCT

    assert ENTRY_1_SIZE_PCT == 0.50
    # PAPER_DOLLARS_PER_ENTRY default = 10000; at price 100 \u2192
    # floor(10000 * 0.50 / 100) = 50 shares (NOT 100).
    monkeypatch.setattr(tg, "PAPER_DOLLARS_PER_ENTRY", 10000.0)
    assert paper_shares_for(100.0) == 50

    # Half of $20k @ price $200 = floor(20000*0.5/200) = 50.
    monkeypatch.setattr(tg, "PAPER_DOLLARS_PER_ENTRY", 20000.0)
    assert paper_shares_for(200.0) == 50

    # Sanity: invalid / zero price returns 0.
    assert paper_shares_for(0.0) == 0
    assert paper_shares_for(-1.0) == 0


def test_entry_1_plus_entry_2_total_is_2x_e1(tg, monkeypatch):
    """v5.13.7 N1: Total position == 2x Entry-1 share count (50/50 by
    share count, per spec L-P3-S6). Total notional drifts with price
    by design — what's invariant is the share count.
    """
    from broker.orders import paper_shares_for

    monkeypatch.setattr(tg, "PAPER_DOLLARS_PER_ENTRY", 10000.0)
    # Entry-1 at price 100 \u2192 50 shares (50% of full).
    e1_shares = paper_shares_for(100.0)
    assert e1_shares == 50

    # Simulate Entry-2 at a slightly different price 110.
    _stub_market(tg, monkeypatch, ticker_price=110.0)
    pos = _make_pos(tg, entry_price=100.0, shares=e1_shares, hwm=105.0)
    # Seed di_prev with one sub-30 print so the subsequent 35 is a cross.
    _stub_market(tg, monkeypatch, ticker_price=104.0)
    monkeypatch.setattr(
        tg,
        "v5_di_1m_5m",
        lambda t: {
            "di_plus_1m": 20.0,
            "di_minus_1m": 0.0,
            "di_plus_5m": 30.0,
            "di_minus_5m": 0.0,
        },
    )
    tg._v5104_maybe_fire_entry_2("AAPL", tg.Side.LONG, pos)
    assert pos["v5104_entry2_fired"] is False

    _stub_market(tg, monkeypatch, ticker_price=110.0)
    monkeypatch.setattr(
        tg,
        "v5_di_1m_5m",
        lambda t: {
            "di_plus_1m": 35.0,
            "di_minus_1m": 0.0,
            "di_plus_5m": 30.0,
            "di_minus_5m": 0.0,
        },
    )
    tg._v5104_maybe_fire_entry_2("AAPL", tg.Side.LONG, pos)
    assert pos["v5104_entry2_fired"] is True

    # v5.13.7 N1: E2 == E1, total == 2x E1.
    assert pos["v5104_entry2_shares"] == e1_shares
    assert pos["shares"] == 2 * e1_shares


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


# ============================================================
# v5.13.7 N1 \u2014 Entry-2 share parity (50/50 by share count)
# ============================================================

def _seed_di_below_30(tg, monkeypatch, ticker_price):
    """Seed di_1m_prev cache with a sub-30 reading so the subsequent
    >30 print counts as an edge cross."""
    _stub_market(tg, monkeypatch, ticker_price=ticker_price)
    monkeypatch.setattr(
        tg,
        "v5_di_1m_5m",
        lambda t: {"di_plus_1m": 20.0, "di_minus_1m": 20.0,
                   "di_plus_5m": 30.0, "di_minus_5m": 30.0},
    )


def _fire_e2_at(tg, monkeypatch, *, side, ticker, pos, price):
    """Fire DI cross to >30 at ``price`` so Entry-2 evaluates."""
    _stub_market(tg, monkeypatch, ticker_price=price)
    monkeypatch.setattr(
        tg,
        "v5_di_1m_5m",
        lambda t: {"di_plus_1m": 35.0, "di_minus_1m": 35.0,
                   "di_plus_5m": 30.0, "di_minus_5m": 30.0},
    )
    tg._v5104_maybe_fire_entry_2(ticker, side, pos)


def test_entry_2_shares_match_entry_1_at_same_price(tg, monkeypatch):
    """v5.13.7 N1: E1 fires 100 shares at $50, E2 fires at the same
    $50 \u2192 E2 == 100 shares (was 100 pre-fix too \u2014 sanity check)."""
    _seed_di_below_30(tg, monkeypatch, 49.0)
    pos = _make_pos(tg, entry_price=50.0, shares=100, hwm=50.5)
    tg._v5104_maybe_fire_entry_2("AAPL", tg.Side.LONG, pos)
    assert pos["v5104_entry2_fired"] is False

    _fire_e2_at(tg, monkeypatch, side=tg.Side.LONG, ticker="AAPL", pos=pos, price=51.0)
    assert pos["v5104_entry2_fired"] is True
    assert pos["v5104_entry2_shares"] == 100
    assert pos["shares"] == 200


def test_entry_2_shares_match_entry_1_at_higher_price(tg, monkeypatch):
    """v5.13.7 N1: E1 fires 100 shares at $50, E2 trigger at $55 \u2192
    E2 STILL == 100 shares. Pre-v5.13.7 was ~91 (dollar parity bug).
    """
    _seed_di_below_30(tg, monkeypatch, 49.0)
    pos = _make_pos(tg, entry_price=50.0, shares=100, hwm=51.0)
    tg._v5104_maybe_fire_entry_2("AAPL", tg.Side.LONG, pos)
    assert pos["v5104_entry2_fired"] is False

    _fire_e2_at(tg, monkeypatch, side=tg.Side.LONG, ticker="AAPL", pos=pos, price=55.0)
    assert pos["v5104_entry2_fired"] is True
    assert pos["v5104_entry2_shares"] == 100  # NOT 91 as before
    assert pos["shares"] == 200


def test_entry_2_total_position_is_2x_e1(tg, monkeypatch):
    """v5.13.7 N1: total == 2 * E1 by share count, regardless of price."""
    _seed_di_below_30(tg, monkeypatch, 49.0)
    pos = _make_pos(tg, entry_price=50.0, shares=77, hwm=51.0)
    _fire_e2_at(tg, monkeypatch, side=tg.Side.LONG, ticker="AAPL", pos=pos, price=58.0)
    assert pos["v5104_entry2_fired"] is True
    assert pos["v5104_entry1_shares"] + pos["v5104_entry2_shares"] == 2 * 77
    assert pos["shares"] == 2 * 77


def _stub_market_short(tg, monkeypatch, *, ticker_price: float, qqq_price: float = 99.0):
    """Section I SHORT requires qqq_5m_close < ema9 AND qqq_last < qqq_avwap."""

    def fake_fetch_1min_bars(t):
        if t == "QQQ":
            return {"current_price": qqq_price, "closes": [qqq_price] * 5, "volumes": [1000] * 5}
        return {"current_price": ticker_price, "closes": [ticker_price] * 5, "volumes": [1000] * 5}

    monkeypatch.setattr(tg, "fetch_1min_bars", fake_fetch_1min_bars)
    monkeypatch.setattr(tg, "get_fmp_quote", lambda t: None)
    monkeypatch.setattr(tg, "_opening_avwap", lambda t: 100.0)

    class _R:
        last_close = qqq_price
        ema9 = qqq_price + 1.0  # close < ema9 \u2192 SHORT permit open

    monkeypatch.setattr(tg, "_QQQ_REGIME", _R())
    monkeypatch.setattr(tg, "_utc_now_iso", lambda: "2026-04-28T15:00:00Z")
    monkeypatch.setattr(tg, "save_paper_state", lambda: None)


def test_entry_2_short_side_shares_match_entry_1(tg, monkeypatch):
    """v5.13.7 N1 mirror (short side): E1 fires 100 shares at $50,
    E2 trigger at $45 (fresh NLOD) \u2192 E2 == 100 shares (NOT
    dollar-parity 111-ish)."""
    # Step 1: seed di_prev below 30 with price ABOVE LWM so the seed
    # call doesn't push LWM down and doesn't fire E2.
    _stub_market_short(tg, monkeypatch, ticker_price=50.5)
    pos = _make_pos(tg, entry_price=50.0, shares=100, hwm=50.0)
    pos["side"] = "SHORT"
    monkeypatch.setattr(
        tg,
        "v5_di_1m_5m",
        lambda t: {"di_plus_1m": 0.0, "di_minus_1m": 20.0,
                   "di_plus_5m": 0.0, "di_minus_5m": 30.0},
    )
    tg._v5104_maybe_fire_entry_2("AAPL", tg.Side.SHORT, pos)
    assert pos["v5104_entry2_fired"] is False

    # Step 2: price drops to 45 (< LWM 50 \u2192 fresh NLOD), DI flips to 35.
    _stub_market_short(tg, monkeypatch, ticker_price=45.0)
    monkeypatch.setattr(
        tg,
        "v5_di_1m_5m",
        lambda t: {"di_plus_1m": 0.0, "di_minus_1m": 35.0,
                   "di_plus_5m": 0.0, "di_minus_5m": 30.0},
    )
    tg._v5104_maybe_fire_entry_2("AAPL", tg.Side.SHORT, pos)
    assert pos["v5104_entry2_fired"] is True
    assert pos["v5104_entry2_shares"] == 100  # NOT a dollar-parity number
    assert pos["shares"] == 200


def test_entry_2_defensive_fallback_when_e1_shares_zero(tg, monkeypatch):
    """v5.13.7 N1 fallback: if e1_shares is 0/missing (Entry-1 didn't
    actually fire), E2 falls back to dollar-parity sizing instead of
    silently sizing to 1 share. Defensive only \u2014 should not happen
    in practice."""
    _seed_di_below_30(tg, monkeypatch, 49.0)
    pos = _make_pos(tg, entry_price=50.0, shares=0, hwm=51.0)
    pos["v5104_entry1_shares"] = 0
    _fire_e2_at(tg, monkeypatch, side=tg.Side.LONG, ticker="AAPL", pos=pos, price=55.0)
    if pos["v5104_entry2_fired"]:
        # Fallback: target_full = floor(10000/55) = 181.
        target_full = int(tg.PAPER_DOLLARS_PER_ENTRY // 55.0)
        assert pos["v5104_entry2_shares"] == max(1, target_full)
