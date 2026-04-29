"""v5.13.9 \u2014 Gate display rewire contract test.

Asserts that `trade_genius._update_gate_snapshot` populates the
dashboard `index` and `polarity` fields by routing through the same
evaluators the entry path uses (`eot_glue.evaluate_section_i` and
`eot_glue.evaluate_boundary_hold_gate`), NOT the legacy v4 PDC
compute.

Boots the bot in SSM_SMOKE_TEST mode (no Telegram, no Alpaca, no
Polygon) so the import path is exercised end-to-end. Then patches
synthetic OR / 1m bar / QQQ regime / 1m close inputs into the live
module and confirms the resulting `_gate_snapshot` row reflects
Section I + boundary_hold semantics.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def smoke_module(monkeypatch):
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    sys.path.insert(0, str(REPO_ROOT))
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius

    yield trade_genius


def _seed_inputs(
    tg,
    monkeypatch,
    *,
    ticker: str,
    price: float,
    or_h: float,
    or_l: float,
    qqq_last: float | None,
    qqq_avwap: float | None,
    qqq_5m_close: float | None,
    qqq_ema9: float | None,
    boundary_closes: list[float],
):
    """Stage every input `_update_gate_snapshot` reads.

    Returns a dict the test can mutate via the returned reference if
    needed (mostly for clarity).
    """
    tg.or_high[ticker] = or_h
    tg.or_low[ticker] = or_l

    monkeypatch.setattr(
        tg,
        "fetch_1min_bars",
        lambda t: (
            {"current_price": price}
            if t == ticker
            else ({"current_price": qqq_last} if t == "QQQ" and qqq_last is not None else None)
        ),
    )
    monkeypatch.setattr(tg, "get_fmp_quote", lambda t: None)
    monkeypatch.setattr(tg, "_opening_avwap", lambda t: qqq_avwap if t == "QQQ" else None)
    monkeypatch.setattr(tg, "tiger_di", lambda t: (None, None))

    tg._QQQ_REGIME.last_close = qqq_5m_close
    tg._QQQ_REGIME.ema9 = qqq_ema9

    # Seed the boundary-hold close ring used by eot_glue.
    import v5_10_1_integration as eot_glue

    eot_glue._last_1m_closes[ticker] = list(boundary_closes)
    # Reset the boundary-hold dedup memory so the test does not depend
    # on whatever a prior test left behind.
    eot_glue._last_boundary_hold.clear()


def test_index_long_open_when_qqq_above_ema9_and_avwap(smoke_module, monkeypatch):
    tg = smoke_module
    ticker = "META"

    # LONG break: price 672 above OR-high 668.995. Two consecutive 1m
    # closes [670.93, 672.09] both above OR-high → boundary_hold true.
    # QQQ 5m close 660.93 > ema9 659.65, current 660.95 > avwap 658.55
    # → Section I long_open true.
    _seed_inputs(
        tg,
        monkeypatch,
        ticker=ticker,
        price=672.0,
        or_h=668.995,
        or_l=665.0,
        qqq_last=660.95,
        qqq_avwap=658.55,
        qqq_5m_close=660.93,
        qqq_ema9=659.65,
        boundary_closes=[670.93, 672.09],
    )

    tg._update_gate_snapshot(ticker)
    snap = tg._gate_snapshot[ticker]

    assert snap["side"] == "LONG"
    assert snap["break"] is True
    assert snap["polarity"] is True, "boundary_hold should be satisfied"
    assert snap["index"] is True, "Section I long_open should be true"


def test_index_long_blocked_when_qqq_below_ema9(smoke_module, monkeypatch):
    tg = smoke_module
    ticker = "AAPL"

    # Same LONG breakout but QQQ 5m close BELOW ema9 → Section I closed.
    # Critically: SPY's PDC plays no role anymore. Even if SPY were
    # above PDC, this should still report index=False because the gate
    # is now QQQ-only.
    _seed_inputs(
        tg,
        monkeypatch,
        ticker=ticker,
        price=200.0,
        or_h=199.0,
        or_l=195.0,
        qqq_last=655.0,
        qqq_avwap=658.55,
        qqq_5m_close=657.0,  # below ema9
        qqq_ema9=659.65,
        boundary_closes=[199.5, 200.0],
    )

    tg._update_gate_snapshot(ticker)
    snap = tg._gate_snapshot[ticker]

    assert snap["side"] == "LONG"
    assert snap["index"] is False, "Section I should be closed when QQQ 5m close < ema9"
    # boundary_hold still satisfied even when index is closed; the two
    # gates are independent on the display.
    assert snap["polarity"] is True


def test_index_none_when_qqq_inputs_not_seeded(smoke_module, monkeypatch):
    tg = smoke_module
    ticker = "AAPL"

    _seed_inputs(
        tg,
        monkeypatch,
        ticker=ticker,
        price=200.0,
        or_h=199.0,
        or_l=195.0,
        qqq_last=None,
        qqq_avwap=None,
        qqq_5m_close=None,
        qqq_ema9=None,
        boundary_closes=[199.5, 200.0],
    )

    tg._update_gate_snapshot(ticker)
    snap = tg._gate_snapshot[ticker]

    assert snap["index"] is None, (
        "index should render as None (yellow/pending) when QQQ regime "
        "or AVWAP is not yet seeded \u2014 never as a hard False"
    )


def test_polarity_none_when_fewer_than_two_closes(smoke_module, monkeypatch):
    tg = smoke_module
    ticker = "AAPL"

    _seed_inputs(
        tg,
        monkeypatch,
        ticker=ticker,
        price=200.0,
        or_h=199.0,
        or_l=195.0,
        qqq_last=660.95,
        qqq_avwap=658.55,
        qqq_5m_close=660.93,
        qqq_ema9=659.65,
        boundary_closes=[],  # no closes yet
    )

    tg._update_gate_snapshot(ticker)
    snap = tg._gate_snapshot[ticker]

    assert snap["polarity"] is None, (
        "polarity should render as None when fewer than 2 closed 1m "
        "candles are available \u2014 not False"
    )


def test_polarity_false_when_only_one_close_outside(smoke_module, monkeypatch):
    tg = smoke_module
    ticker = "AAPL"

    # Two closes but the older one is INSIDE the OR \u2014 boundary_hold
    # requires BOTH to be strictly outside.
    _seed_inputs(
        tg,
        monkeypatch,
        ticker=ticker,
        price=200.0,
        or_h=199.0,
        or_l=195.0,
        qqq_last=660.95,
        qqq_avwap=658.55,
        qqq_5m_close=660.93,
        qqq_ema9=659.65,
        boundary_closes=[198.5, 199.5],  # 198.5 is below or_high (inside)
    )

    tg._update_gate_snapshot(ticker)
    snap = tg._gate_snapshot[ticker]

    assert snap["polarity"] is False
    # index is independent and should still be true.
    assert snap["index"] is True


def test_short_side_uses_short_arms_of_both_gates(smoke_module, monkeypatch):
    tg = smoke_module
    ticker = "TSLA"

    # SHORT break: price 240 below OR-low 245. Two closes both strictly
    # below OR-low → boundary_hold(SHORT) satisfied. QQQ 5m close 657
    # < ema9 659.65, QQQ price 655 < AVWAP 658.55 → Section I
    # short_open true.
    _seed_inputs(
        tg,
        monkeypatch,
        ticker=ticker,
        price=240.0,
        or_h=250.0,
        or_l=245.0,
        qqq_last=655.0,
        qqq_avwap=658.55,
        qqq_5m_close=657.0,
        qqq_ema9=659.65,
        boundary_closes=[244.5, 240.0],
    )

    tg._update_gate_snapshot(ticker)
    snap = tg._gate_snapshot[ticker]

    assert snap["side"] == "SHORT"
    assert snap["break"] is True
    assert snap["polarity"] is True
    assert snap["index"] is True


def test_no_pdc_consumed_by_gate_snapshot(smoke_module, monkeypatch):
    """v5.13.9 contract: _update_gate_snapshot must NOT consult the
    `pdc` dict for index or polarity. We seed an obviously-wrong PDC
    and verify the snapshot still reflects the correct Section I /
    boundary_hold answers."""
    tg = smoke_module
    ticker = "AAPL"

    # Wildly inconsistent PDC values. If any of them leak into the
    # display compute we'll see it in `index` or `polarity`.
    tg.pdc["SPY"] = 9999.0
    tg.pdc["QQQ"] = 9999.0
    tg.pdc[ticker] = 9999.0

    _seed_inputs(
        tg,
        monkeypatch,
        ticker=ticker,
        price=200.0,
        or_h=199.0,
        or_l=195.0,
        qqq_last=660.95,
        qqq_avwap=658.55,
        qqq_5m_close=660.93,
        qqq_ema9=659.65,
        boundary_closes=[199.5, 200.0],
    )

    tg._update_gate_snapshot(ticker)
    snap = tg._gate_snapshot[ticker]

    # PDC = 9999 would have made the legacy index/polarity False. The
    # rewired version ignores PDC entirely.
    assert snap["index"] is True
    assert snap["polarity"] is True

    # Cleanup so we don't poison the next test in the module.
    tg.pdc.pop("SPY", None)
    tg.pdc.pop("QQQ", None)
    tg.pdc.pop(ticker, None)


def test_regime_bullish_global_removed(smoke_module):
    """v5.13.9 contract: the `_regime_bullish` module global was
    retired alongside the PDC regime alert in engine/scan.py."""
    tg = smoke_module
    assert not hasattr(tg, "_regime_bullish"), (
        "_regime_bullish should have been removed in v5.13.9 (it was "
        "only consumed by the now-deleted PDC regime alert)"
    )
