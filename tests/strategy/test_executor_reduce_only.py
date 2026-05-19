"""v9.1.125 -- regression test for the EOD close cap-bypass.

The 2026-05-18 incident:
  - Val's live $30k account had $29k notional in the EOD entry pair
    (ORCL long + NFLX short, 95% of equity).
  - V10 EOD engine's close fired but `[V10-FIRE] notional cap` clamped
    the closing orders to 0 shares (cumulative notional already at cap).
  - Both close orders returned `submitted=False`.
  - Position only flat thanks to Alpaca's broker-side EOD auto-flush.

These tests pin the v9.1.125 fix so the regression doesn't reappear.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _account(equity: float, long_mv: float = 0.0, short_mv: float = 0.0):
    """Simulate an Alpaca trading client's get_account() return."""
    return SimpleNamespace(
        equity=str(equity),
        cash=str(equity - long_mv - abs(short_mv)),
        long_market_value=str(long_mv),
        short_market_value=str(-abs(short_mv)),
    )


def _build_executor():
    """Build a minimal TradeGeniusBase-like instance for fire_* tests.

    We avoid the full executor bootstrap (Alpaca client, Telegram, state
    files) by monkeypatching the bits fire_long/fire_short need.
    """
    from executors.base import TradeGeniusBase

    # Bypass __init__ -- it loads state files and starts threads
    ex = TradeGeniusBase.__new__(TradeGeniusBase)
    ex.NAME = "TestExec"
    ex.mode = "live"
    ex.client = MagicMock()
    ex._open_positions = {}
    ex._client_order_id_used = set()
    return ex


# ---------------------------------------------------------------------------
# 1. Entry path (reduce_only=False) still respects the cap
# ---------------------------------------------------------------------------


def test_entry_respects_cumulative_cap_when_at_limit():
    """Default fire_long (reduce_only=False) must still clamp to 0 shares
    when cumulative notional already hits 95% equity. Pre-v9.1.125
    behavior preserved."""
    from executors.base import TradeGeniusBase

    ex = _build_executor()
    # Account: $30k equity, $29.5k of notional already used (98%)
    ex.client.get_account.return_value = _account(equity=30_000, long_mv=15_000, short_mv=14_500)

    with patch.object(TradeGeniusBase, "_build_client_order_id", return_value="c"):
        with patch.object(ex, "client") as _mc:
            _mc.get_account.return_value = _account(30_000, 15_000, 14_500)
            _mc.submit_order.return_value = SimpleNamespace(id="ord1")
            ex.client = _mc
            ok = ex.fire_long(
                "AAPL", price=200.0, shares=100, error_callback=None, reduce_only=False
            )
    # Cumulative cap blocks: remaining = 0.95*30k - 29.5k = -1k -> clamped to 0 shares.
    assert ok is False


def test_entry_partially_clamps_when_some_remaining():
    """Entry with partial cap headroom should clamp but still submit."""
    from executors.base import TradeGeniusBase

    ex = _build_executor()
    # $30k equity, $20k used; remaining = 0.95*30k - 20k = $8500.
    # At price=$200 -> 42 shares max. Caller asked for 100.
    mock_client = MagicMock()
    mock_client.get_account.return_value = _account(30_000, 20_000, 0)
    mock_client.submit_order.return_value = SimpleNamespace(id="ord1")
    ex.client = mock_client

    with patch.object(TradeGeniusBase, "_build_client_order_id", return_value="c"):
        ok = ex.fire_long("AAPL", price=200.0, shares=100, error_callback=None, reduce_only=False)
    assert ok is True  # submitted, just clamped
    # Verify the qty was clamped down (~42 shares, not 100)
    submitted_req = mock_client.submit_order.call_args[0][0]
    assert submitted_req.qty <= 42
    assert submitted_req.qty >= 40  # allow 1-2 rounding slack


# ---------------------------------------------------------------------------
# 2. Close path (reduce_only=True) bypasses the cap
# ---------------------------------------------------------------------------


def test_close_bypasses_cap_at_full_utilization():
    """The actual 2026-05-18 scenario: account at 95% utilization, a close
    order MUST submit at full requested qty when reduce_only=True."""
    from executors.base import TradeGeniusBase

    ex = _build_executor()
    # Reproduce Val's state on 2026-05-18 16:00 ET:
    #   equity=$30,635, long_mv=$36,300 (ORCL), short_mv=$36,300 (NFLX abs)
    #   used = $72,600; cap = 0.95 * $30,635 = $29,103. Massively over.
    mock_client = MagicMock()
    mock_client.get_account.return_value = _account(30_635, 36_300, 36_300)
    mock_client.submit_order.return_value = SimpleNamespace(id="ord1")
    ex.client = mock_client

    with patch.object(TradeGeniusBase, "_build_client_order_id", return_value="c"):
        # Close the ORCL long: sell 190 shares.
        ok = ex.fire_short("ORCL", price=186.62, shares=190, error_callback=None, reduce_only=True)

    # Without v9.1.125 this returned False with submitted=False.
    # With v9.1.125 the cap is bypassed.
    assert ok is True
    submitted_req = mock_client.submit_order.call_args[0][0]
    assert submitted_req.qty == 190  # NOT clamped


def test_close_bypasses_cap_long_side():
    """Mirror of the above: closing a SHORT (cover by buying) at full util."""
    from executors.base import TradeGeniusBase

    ex = _build_executor()
    mock_client = MagicMock()
    mock_client.get_account.return_value = _account(30_635, 36_300, 36_300)
    mock_client.submit_order.return_value = SimpleNamespace(id="ord2")
    ex.client = mock_client

    with patch.object(TradeGeniusBase, "_build_client_order_id", return_value="c"):
        ok = ex.fire_long("NFLX", price=89.66, shares=401, error_callback=None, reduce_only=True)

    assert ok is True
    submitted_req = mock_client.submit_order.call_args[0][0]
    assert submitted_req.qty == 401


def test_reduce_only_default_is_false():
    """Belt-and-suspenders: callers that don't explicitly opt in must NOT
    accidentally bypass the cap. The kwarg default is False."""
    from executors.base import TradeGeniusBase

    ex = _build_executor()
    mock_client = MagicMock()
    mock_client.get_account.return_value = _account(30_000, 28_500, 0)  # 95%
    mock_client.submit_order.return_value = SimpleNamespace(id="ord3")
    ex.client = mock_client

    with patch.object(TradeGeniusBase, "_build_client_order_id", return_value="c"):
        # No reduce_only kwarg passed at all -- should hit cap and reject.
        ok = ex.fire_long("AAPL", price=200.0, shares=100, error_callback=None)
    assert ok is False


# ---------------------------------------------------------------------------
# 3. Dispatch + EOD close paths
# ---------------------------------------------------------------------------


def test_dispatch_forwards_reduce_only_to_executor():
    """_v10_dispatch_executor_fire(reduce_only=True) must pass through."""
    import os

    os.environ.pop("ORB_LIVE_MODE", None)

    from engine import scan as _scan

    fake_ex = MagicMock()
    fake_ex.fire_long.return_value = True
    fake_ex.fire_short.return_value = True

    with patch("executors.bootstrap.get_executor", return_value=fake_ex):
        _scan._v10_dispatch_executor_fire(
            pid="val",
            side="short",
            ticker="ORCL",
            price=186.62,
            shares=190,
            callbacks=None,
            reduce_only=True,
        )
    # Verify reduce_only=True was forwarded to fire_short
    fake_ex.fire_short.assert_called_once()
    call_kwargs = fake_ex.fire_short.call_args.kwargs
    assert call_kwargs.get("reduce_only") is True


def test_eod_fire_broker_close_sets_reduce_only():
    """_eod_fire_broker_close must ALWAYS pass reduce_only=True so EOD
    closes never hit the cap. This is the root-cause pin -- if a future
    refactor strips this kwarg, the test catches it."""
    from engine import scan as _scan

    with patch.object(_scan, "_v10_dispatch_executor_fire") as mock_dispatch:
        _scan._eod_fire_broker_close(
            callbacks=None,
            pid="val",
            ticker="ORCL",
            side="long",
            price=186.62,
            shares=190,
        )
    mock_dispatch.assert_called_once()
    call_kwargs = mock_dispatch.call_args.kwargs
    assert call_kwargs.get("reduce_only") is True
    assert call_kwargs.get("side") == "short"  # long position -> sell to close
    assert call_kwargs.get("shares") == 190
