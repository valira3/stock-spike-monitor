"""v6.15.0 broker fidelity: _close_position_idempotent now routes by reason.

Pre-v6.15.0 the close path called ``client.close_position(ticker)`` for
every reason, ignoring the carefully-built broker.order_types mapping.
This test pins the new behaviour: each reason class submits the
spec-correct request type, and the legacy MARKET fallback is reachable
only when prerequisites (side/qty) are missing.

Cases (all on a 24-share LONG MSFT @ 415.40 with stop 413.32):
  1. sentinel_a_stop_price -> StopLimitOrderRequest (stop=413.32, lim=412.08)
  2. sentinel_b_ema_cross   -> LimitOrderRequest IOC (when bid/ask available)
  3. sentinel_r2_hard_stop  -> StopOrderRequest (stop=413.32)
  4. EOD                    -> MarketOrderRequest
  5. side missing on row    -> falls through to legacy close_position
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _make_executor(monkeypatch):
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    monkeypatch.setenv("TEST_ALPACA_PAPER_KEY", "dummy_paper_key")
    monkeypatch.setenv("TEST_ALPACA_PAPER_SECRET", "dummy_paper_secret")
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius  # noqa: F401
    from executors import TradeGeniusBase
    from executors import base as base_mod

    monkeypatch.setattr(base_mod, "RECONCILE_RETRY_SLEEP", 0.0)
    monkeypatch.setattr(base_mod, "RECONCILE_GRACE_SECONDS", 0.5)

    class _StubExec(TradeGeniusBase):
        NAME = "TestStub"
        ENV_PREFIX = "TEST_"

    inst = _StubExec()
    inst.dollars_per_entry = 10_000.0

    submitted = []
    legacy_calls = []

    class _FakeClient:
        def get_account(self):
            return types.SimpleNamespace(equity=200_000.0, cash=200_000.0)

        def submit_order(self, req):
            submitted.append(req)
            return types.SimpleNamespace(id="fake-order-id")

        def close_position(self, ticker):
            # Should only be hit on the legacy fallback path.
            legacy_calls.append(ticker)

        def get_open_position(self, ticker):
            raise Exception('{"code":40410000}')

        def get_all_positions(self):
            return []

    fc = _FakeClient()
    inst._ensure_client = lambda: fc  # type: ignore
    inst._send_own_telegram = lambda _msg: None  # type: ignore
    inst._submit_order_idempotent = lambda client, req, coid: client.submit_order(req)  # type: ignore
    inst._build_client_order_id = lambda ticker, side: f"{ticker}-{side}-coid"  # type: ignore
    inst._persist_position = lambda _t: None  # type: ignore
    inst._delete_persisted_position = lambda _t: None  # type: ignore
    # Skip the post-EXIT broker reconcile; this test only cares about
    # the order request that was submitted.
    inst._reconcile_position_with_broker = lambda *a, **kw: None  # type: ignore

    return inst, fc, submitted, legacy_calls


def _seed_long_msft(inst, *, with_stop=True, with_side=True):
    inst.positions["MSFT"] = {
        "ticker": "MSFT",
        "side": "LONG" if with_side else None,
        "qty": 24,
        "entry_price": 415.40,
        "entry_ts_utc": "2026-05-05T15:00:00+00:00",
        "source": "SIGNAL",
        "stop": 413.32 if with_stop else None,
        "trail": None,
    }


# ---------------------------------------------------------------------------
# Case 1: sentinel_a_stop_price -> StopLimitOrderRequest
# ---------------------------------------------------------------------------


def test_price_stop_routes_to_stop_limit_request(monkeypatch):
    inst, fc, submitted, legacy = _make_executor(monkeypatch)
    _seed_long_msft(inst)

    inst._close_position_idempotent(
        fc, "MSFT", "label", "sentinel_a_stop_price"
    )

    assert legacy == [], "STOP_LIMIT path must not call legacy close_position"
    assert len(submitted) == 1
    req = submitted[0]
    cls_name = type(req).__name__
    assert cls_name == "StopLimitOrderRequest", f"got {cls_name}"
    # 30 bps slip on a 413.32 stop -> 413.32 * 0.997 = 412.080 -> rounds 412.08
    assert float(req.stop_price) == pytest.approx(413.32)
    assert float(req.limit_price) == pytest.approx(round(413.32 * 0.997, 2))
    # MSFT position cleared after submit.
    assert "MSFT" not in inst.positions


# ---------------------------------------------------------------------------
# Case 2: sentinel_b_ema_cross -> LimitOrderRequest IOC (with bid/ask)
# ---------------------------------------------------------------------------


def test_alarm_b_routes_to_limit_ioc_when_quote_available(monkeypatch):
    inst, fc, submitted, legacy = _make_executor(monkeypatch)
    _seed_long_msft(inst)

    # Stub quote snapshot on the trade_genius module so the sentinel
    # LIMIT path can compute its limit price.
    import trade_genius as _tg

    monkeypatch.setattr(_tg, "_v512_quote_snapshot", lambda _t: (414.50, 414.60))

    inst._close_position_idempotent(fc, "MSFT", "label", "sentinel_b_ema_cross")

    assert legacy == []
    assert len(submitted) == 1
    req = submitted[0]
    assert type(req).__name__ == "LimitOrderRequest"
    # LONG exit at bid * 0.995 = 414.50 * 0.995 = 412.4275 -> 412.43
    assert float(req.limit_price) == pytest.approx(round(414.50 * 0.995, 2))
    # IOC, not DAY.
    from alpaca.trading.enums import TimeInForce
    assert req.time_in_force == TimeInForce.IOC


# ---------------------------------------------------------------------------
# Case 3: R-2 hard stop -> StopOrderRequest (STOP_MARKET)
# ---------------------------------------------------------------------------


def test_r2_hard_stop_routes_to_stop_market_request(monkeypatch):
    inst, fc, submitted, legacy = _make_executor(monkeypatch)
    _seed_long_msft(inst)

    inst._close_position_idempotent(
        fc, "MSFT", "label", "sentinel_r2_hard_stop"
    )

    assert legacy == []
    assert len(submitted) == 1
    req = submitted[0]
    assert type(req).__name__ == "StopOrderRequest"
    assert float(req.stop_price) == pytest.approx(413.32)


# ---------------------------------------------------------------------------
# Case 4: EOD -> MarketOrderRequest (explicit, with order_id)
# ---------------------------------------------------------------------------


def test_eod_routes_to_market_request(monkeypatch):
    inst, fc, submitted, legacy = _make_executor(monkeypatch)
    _seed_long_msft(inst)

    inst._close_position_idempotent(fc, "MSFT", "label", "EOD")

    assert legacy == []
    assert len(submitted) == 1
    req = submitted[0]
    assert type(req).__name__ == "MarketOrderRequest"
    assert int(req.qty) == 24


# ---------------------------------------------------------------------------
# Case 5: side missing on position row -> legacy fallback.
# ---------------------------------------------------------------------------


def test_legacy_fallback_when_position_row_missing_side(monkeypatch):
    inst, fc, submitted, legacy = _make_executor(monkeypatch)
    _seed_long_msft(inst, with_side=False)

    inst._close_position_idempotent(
        fc, "MSFT", "label", "sentinel_a_stop_price"
    )

    # Typed-request path skipped; legacy close_position called once.
    assert submitted == []
    assert legacy == ["MSFT"]
    assert "MSFT" not in inst.positions
