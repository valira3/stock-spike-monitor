"""v7.0.4 \\u2014 dashboard executor trades FIFO pairing tests.

Covers the behaviour fixed in v7.0.4: the per-executor /api/executor/<name>
payload now pairs Alpaca fills per symbol so SELL/COVER rows on the Val and
Gene Today's Trades panel carry pnl, pnl_pct, and entry_price the same way
Main's _today_trades does. Before this fix every closing fill rendered with
a "\\u2014" P&L tail because the upstream payload omitted those fields.

Tests target dashboard_server._pair_executor_fills directly so they don't
depend on Alpaca SDK objects, network, or executor instances.
"""
from __future__ import annotations

import pytest

from dashboard_server import _pair_executor_fills


def _fill(side_str, sym, qty, price, fiso):
    return {
        "side_str": side_str,
        "sym": sym,
        "qty": qty,
        "price": price,
        "ftime": fiso[11:16],
        "fiso": fiso,
        "fdate": fiso[:10],
    }


def test_long_round_trip_pairs_pnl():
    """LONG entry + SELL pairs with positive pnl when price rose."""
    fills = [
        _fill("buy",  "AAPL", 10, 150.00, "2026-05-06T10:00:00-04:00"),
        _fill("sell", "AAPL", 10, 152.50, "2026-05-06T10:30:00-04:00"),
    ]
    out = _pair_executor_fills(fills)
    assert len(out) == 2
    assert out[0]["action"] == "BUY"
    assert out[0]["side"] == "LONG"
    assert out[0].get("pnl") is None  # opens never carry pnl
    assert out[1]["action"] == "SELL"
    assert out[1]["side"] == "LONG"
    assert out[1]["pnl"] == 25.00  # (152.50 - 150.00) * 10
    # pnl_pct is rounded to 2dp in the helper (matches dashboard display).
    assert abs(out[1]["pnl_pct"] - round((152.50 / 150.00 - 1) * 100, 2)) < 1e-9
    assert out[1]["entry_price"] == 150.00
    assert out[1]["exit_price"] == 152.50


def test_short_round_trip_emits_short_then_cover():
    """SELL from flat opens a SHORT; subsequent BUY closes it as COVER."""
    fills = [
        _fill("sell", "TSLA", 5, 220.00, "2026-05-06T09:50:00-04:00"),
        _fill("buy",  "TSLA", 5, 215.00, "2026-05-06T10:15:00-04:00"),
    ]
    out = _pair_executor_fills(fills)
    assert [r["action"] for r in out] == ["SHORT", "COVER"]
    assert [r["side"] for r in out] == ["SHORT", "SHORT"]
    # Short P&L: short at 220, cover at 215 -> +25 profit.
    assert out[1]["pnl"] == 25.00
    # pnl_pct for short: (entry/exit - 1) * 100 = (220/215 - 1)*100
    assert abs(out[1]["pnl_pct"] - ((220.00 / 215.00 - 1) * 100)) < 1e-2
    assert out[1]["entry_price"] == 220.00


def test_short_loss_negative_pnl():
    """Short that covers higher than entry shows negative pnl."""
    fills = [
        _fill("sell", "NVDA", 4, 100.00, "2026-05-06T09:50:00-04:00"),
        _fill("buy",  "NVDA", 4, 102.00, "2026-05-06T10:15:00-04:00"),
    ]
    out = _pair_executor_fills(fills)
    assert out[1]["action"] == "COVER"
    assert out[1]["side"] == "SHORT"
    assert out[1]["pnl"] == -8.00  # (100 - 102) * 4


def test_stack_on_average_basis():
    """Two BUYs followed by one SELL average the entry basis FIFO."""
    fills = [
        _fill("buy",  "MSFT", 4, 400.00, "2026-05-06T09:50:00-04:00"),
        _fill("buy",  "MSFT", 6, 410.00, "2026-05-06T10:00:00-04:00"),
        _fill("sell", "MSFT", 10, 415.00, "2026-05-06T10:30:00-04:00"),
    ]
    out = _pair_executor_fills(fills)
    assert [r["action"] for r in out] == ["BUY", "BUY", "SELL"]
    # Avg entry over 10 shares: (4*400 + 6*410)/10 = 406
    assert out[2]["entry_price"] == 406.0
    # PnL: (415 - 406) * 10 = 90
    assert out[2]["pnl"] == 90.00


def test_partial_close_leaves_residual_lot():
    """A SELL of fewer shares than the open prices off FIFO front."""
    fills = [
        _fill("buy",  "GOOG", 8, 200.00, "2026-05-06T09:50:00-04:00"),
        _fill("sell", "GOOG", 3, 210.00, "2026-05-06T10:00:00-04:00"),
        # The remaining 5 shares should still be held; second sell
        # at 195 should produce a -25 loss off the 200 basis.
        _fill("sell", "GOOG", 5, 195.00, "2026-05-06T10:30:00-04:00"),
    ]
    out = _pair_executor_fills(fills)
    closes = [r for r in out if r["action"] == "SELL"]
    assert len(closes) == 2
    assert closes[0]["pnl"] == 30.00   # (210 - 200) * 3
    assert closes[1]["pnl"] == -25.00  # (195 - 200) * 5


def test_side_flip_long_then_short_same_symbol():
    """After fully closing a LONG, opening a SHORT on the same symbol
    flips the book without contaminating the new lot's basis."""
    fills = [
        _fill("buy",  "META", 5, 500.00, "2026-05-06T09:50:00-04:00"),
        _fill("sell", "META", 5, 510.00, "2026-05-06T10:00:00-04:00"),  # closes LONG
        _fill("sell", "META", 5, 515.00, "2026-05-06T10:15:00-04:00"),  # opens SHORT
        _fill("buy",  "META", 5, 512.00, "2026-05-06T10:30:00-04:00"),  # COVER
    ]
    out = _pair_executor_fills(fills)
    actions = [r["action"] for r in out]
    sides = [r["side"] for r in out]
    assert actions == ["BUY", "SELL", "SHORT", "COVER"]
    assert sides == ["LONG", "LONG", "SHORT", "SHORT"]
    # SHORT entry basis must be 515 (NOT mixed with the prior LONG).
    cover = out[3]
    assert cover["entry_price"] == 515.0
    # Profit on short: 515 - 512 = +3 per share, * 5 = 15.
    assert cover["pnl"] == 15.00


def test_chrono_sort_resilient_to_input_order():
    """Helper sorts by fiso so out-of-order input still pairs correctly."""
    fills = [
        _fill("sell", "AAPL", 10, 152.50, "2026-05-06T10:30:00-04:00"),
        _fill("buy",  "AAPL", 10, 150.00, "2026-05-06T10:00:00-04:00"),
    ]
    out = _pair_executor_fills(fills)
    assert out[0]["action"] == "BUY"
    assert out[1]["action"] == "SELL"
    assert out[1]["pnl"] == 25.00


def test_closes_carry_required_dashboard_fields():
    """Frontend renderExecTrades reads pnl, pnl_pct, side, action; verify
    every close row carries them so the Today's Trades panel renders P&L."""
    fills = [
        _fill("buy",  "AVGO", 3, 429.36, "2026-05-06T09:47:00-04:00"),
        _fill("sell", "AVGO", 3, 429.22, "2026-05-06T09:47:30-04:00"),
    ]
    out = _pair_executor_fills(fills)
    close = out[1]
    for required in ("action", "side", "ticker", "shares", "price",
                     "pnl", "pnl_pct", "entry_price", "exit_price",
                     "filled_at", "time", "date"):
        assert required in close, f"close missing {required}"
    # pnl can be slightly negative for a quick scratch
    assert close["action"] == "SELL"
    assert close["side"] == "LONG"
    assert close["pnl"] == round((429.22 - 429.36) * 3, 2)


def test_close_without_prior_open_does_not_crash():
    """An orphan SELL (no prior BUY for the symbol) is handled gracefully:
    treated as opening a SHORT (as if the fill is the open). This is the
    same behaviour the FIFO rule produces and it never crashes."""
    fills = [
        _fill("sell", "ORPH", 1, 100.00, "2026-05-06T10:00:00-04:00"),
    ]
    out = _pair_executor_fills(fills)
    # No prior open -> this fill itself opens a SHORT.
    assert out[0]["action"] == "SHORT"
    assert out[0]["side"] == "SHORT"


def test_empty_input_returns_empty_list():
    assert _pair_executor_fills([]) == []


def test_open_rows_have_no_pnl():
    """Opens (BUY / SHORT) must not carry pnl/pnl_pct so the running
    realized total only sums close rows."""
    fills = [
        _fill("buy",  "QQQ", 2, 500.00, "2026-05-06T09:50:00-04:00"),
        _fill("sell", "SPY", 3, 580.00, "2026-05-06T09:55:00-04:00"),
    ]
    out = _pair_executor_fills(fills)
    for row in out:
        if row["action"] in ("BUY", "SHORT"):
            assert "pnl" not in row
            assert "pnl_pct" not in row
