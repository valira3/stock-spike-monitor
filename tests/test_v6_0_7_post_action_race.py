"""v6.0.7 - post-action reconcile race fix.

Alpaca's REST get_open_position is eventually consistent. After ENTRY
submit, it can return 40410000 ("position not found") for ~1-2 s before
the fill propagates; after EXIT submit it can still return the pre-cover
position for ~1-2 s. Pre-v6.0.7, the post-action reconcile took the
first response at face value, which:

  * deleted local rows the ENTRY just created (post-ENTRY race), and
  * grafted phantom POST_RECONCILE rows for positions just covered
    (post-EXIT race).

These tests pin the new behaviour:

  * post-ENTRY reconcile retries while broker says flat inside a grace
    window, and after the window leaves the local row alone.
  * post-EXIT reconcile retries while broker still has the position
    inside a grace window, and after the window leaves it untracked.
  * legacy "any" caller (periodic sweep) still single-shot.
  * an ENTRY that races and then settles inside the window completes
    cleanly (broker view wins, local row qty/entry are overwritten).
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

    # Disable real sleeps + shrink grace so tests run fast. Tests that
    # need a non-zero grace can re-set these locally.
    monkeypatch.setattr(base_mod, "RECONCILE_RETRY_SLEEP", 0.0)
    monkeypatch.setattr(base_mod, "RECONCILE_GRACE_SECONDS", 0.5)

    class _StubExec(TradeGeniusBase):
        NAME = "TestStub"
        ENV_PREFIX = "TEST_"

    inst = _StubExec()
    inst.dollars_per_entry = 10_000.0

    class _FakePos:
        def __init__(self, symbol, qty, avg_entry_price):
            self.symbol = symbol
            self.qty = qty
            self.avg_entry_price = avg_entry_price

    state = {
        # responses: list of "present" / "flat" / Exception, popped in order.
        "responses": [],
        "calls": [],
        "broker_book": {},
    }

    class _FakeClient:
        def get_account(self):
            return types.SimpleNamespace(equity=200_000.0, cash=200_000.0, buying_power=400_000.0)

        def submit_order(self, req):
            return types.SimpleNamespace(id="fake-order-id")

        def close_position(self, ticker):
            state["broker_book"].pop(ticker, None)

        def close_all_positions(self, cancel_orders=False):
            state["broker_book"].clear()

        def get_open_position(self, ticker):
            state["calls"].append(ticker)
            if state["responses"]:
                r = state["responses"].pop(0)
                if isinstance(r, Exception):
                    raise r
                if r == "flat":
                    raise Exception(
                        '{"code":40410000,"message":"position not found: ' + ticker + '"}'
                    )
                if r == "present":
                    spec = state["broker_book"].get(ticker)
                    if spec is None:
                        raise Exception('{"code":40410000,"message":"position not found"}')
                    return _FakePos(ticker, spec["qty"], spec["avg_entry_price"])
            # Default: read broker_book.
            spec = state["broker_book"].get(ticker)
            if spec is None:
                raise Exception('{"code":40410000,"message":"position not found"}')
            return _FakePos(ticker, spec["qty"], spec["avg_entry_price"])

        def get_all_positions(self):
            return [
                _FakePos(t, spec["qty"], spec["avg_entry_price"])
                for t, spec in state["broker_book"].items()
            ]

    fc = _FakeClient()
    inst._ensure_client = lambda: fc  # type: ignore
    inst._send_own_telegram = lambda _msg: None  # type: ignore
    inst._submit_order_idempotent = lambda client, req, coid: client.submit_order(req)  # type: ignore
    inst._build_client_order_id = lambda ticker, side: f"{ticker}-{side}-coid"  # type: ignore
    inst._persist_position = lambda _t: None  # type: ignore
    inst._delete_persisted_position = lambda _t: None  # type: ignore

    return inst, fc, state


# ---------------------------------------------------------------------------
# Post-ENTRY race: 40410000 inside grace must NOT delete the local row.
# ---------------------------------------------------------------------------


def test_post_entry_race_does_not_delete_local_row(monkeypatch):
    """Reproduces the v6.0.6 MSFT bug: ENTRY posts, broker returns 40410000
    one second later (eventual consistency), then settles. Pre-fix this
    deleted the freshly-created local row. Post-fix the row must survive
    and be overwritten by the broker view once the broker settles.
    """
    inst, fc, state = _make_executor(monkeypatch)
    # Simulate: broker returns flat for first 2 polls, then settles to present.
    state["broker_book"]["MSFT"] = {"qty": 24, "avg_entry_price": 415.40}
    state["responses"] = ["flat", "flat", "present"]

    inst._record_position("MSFT", "LONG", 24, 415.32)
    inst._reconcile_position_with_broker("MSFT", expect="present")

    assert "MSFT" in inst.positions, "post-ENTRY race must NOT delete the local row"
    assert inst.positions["MSFT"]["qty"] == 24
    assert abs(inst.positions["MSFT"]["entry_price"] - 415.40) < 1e-6
    assert len(state["calls"]) == 3, f"expected 3 polls (2 flat + 1 present), got {state['calls']}"


def test_post_entry_grace_expires_keeps_row(monkeypatch):
    """If the broker is STILL flat after the grace window expires, the
    safest move is to keep the local row in place and let the next
    periodic reconcile resolve a real divergence - deleting the row
    would mask a real submit failure that the alarm path can catch.
    """
    inst, fc, state = _make_executor(monkeypatch)
    # Broker is flat for every poll inside the grace window.
    state["responses"] = ["flat"] * 50

    inst._record_position("MSFT", "LONG", 24, 415.32)
    inst._reconcile_position_with_broker("MSFT", expect="present")

    # Local row must survive; let the next periodic sweep heal a real flat.
    assert "MSFT" in inst.positions, "post-ENTRY grace expiry must NOT delete the local row"
    assert inst.positions["MSFT"]["source"] == "SIGNAL"


# ---------------------------------------------------------------------------
# Post-EXIT race: still-has-position inside grace must NOT graft a phantom.
# ---------------------------------------------------------------------------


def test_post_exit_race_does_not_graft_phantom_row(monkeypatch):
    """Reproduces the v6.0.6 NFLX bug: EXIT covers the position, broker
    still reports it open one second later (eventual consistency), then
    settles to flat. Pre-fix this grafted a phantom POST_RECONCILE row.
    Post-fix the helper must wait, see flat, and leave the position out.
    """
    inst, fc, state = _make_executor(monkeypatch)
    # Broker still has it for 2 polls, then flat.
    state["broker_book"]["NFLX"] = {"qty": -54, "avg_entry_price": 92.15}
    # On poll 3, simulate the cover finally settling.

    def _drain_after_two_polls(t):
        state["calls"].append(t)
        if len(state["calls"]) <= 2:
            spec = state["broker_book"][t]
            from types import SimpleNamespace

            return SimpleNamespace(
                symbol=t, qty=spec["qty"], avg_entry_price=spec["avg_entry_price"]
            )
        raise Exception('{"code":40410000,"message":"position not found"}')

    fc.get_open_position = _drain_after_two_polls

    # The local row was already removed by _close_position_idempotent.
    assert "NFLX" not in inst.positions
    inst._stamp_action("NFLX")  # post-EXIT timestamp like the real path
    inst._reconcile_position_with_broker("NFLX", expect="flat")

    assert "NFLX" not in inst.positions, "post-EXIT race must NOT graft a phantom row"
    assert len(state["calls"]) == 3, (
        f"expected 3 polls (2 still-has + 1 flat), got {state['calls']}"
    )


def test_post_exit_grace_expires_does_not_graft(monkeypatch):
    """If the broker STILL has the position after the grace window
    expires, the close did not take. Do NOT graft a phantom row -
    leave it untracked and let the next signal heal a real divergence
    (a graft would dress up a failed close as a successful one).
    """
    inst, fc, state = _make_executor(monkeypatch)
    state["broker_book"]["NFLX"] = {"qty": -54, "avg_entry_price": 92.15}
    # Broker keeps reporting present forever inside the window.

    inst._stamp_action("NFLX")
    inst._reconcile_position_with_broker("NFLX", expect="flat")

    assert "NFLX" not in inst.positions, "post-EXIT grace expiry must NOT graft a phantom row"


# ---------------------------------------------------------------------------
# Legacy single-shot path preserved for periodic sweeps.
# ---------------------------------------------------------------------------


def test_periodic_sweep_any_is_single_shot(monkeypatch):
    """expect=\"any\" preserves pre-v6.0.7 behaviour: one call, no retry."""
    inst, fc, state = _make_executor(monkeypatch)
    state["responses"] = ["flat", "present"]  # if this called twice we'd see present
    state["broker_book"]["AAPL"] = {"qty": 10, "avg_entry_price": 270.00}

    inst._record_position("AAPL", "LONG", 10, 270.00)
    inst._reconcile_position_with_broker("AAPL", expect="any")

    assert len(state["calls"]) == 1, (
        f"any-mode must be single-shot; got {len(state['calls'])} calls"
    )
    # First (single) shot saw flat - legacy path drops the row.
    assert "AAPL" not in inst.positions


def test_default_expect_is_any(monkeypatch):
    """Calling without expect= keeps pre-v6.0.7 single-shot semantics."""
    inst, fc, state = _make_executor(monkeypatch)
    state["responses"] = ["flat"]
    inst._record_position("AAPL", "LONG", 10, 270.00)

    inst._reconcile_position_with_broker("AAPL")  # no expect=

    assert len(state["calls"]) == 1
    assert "AAPL" not in inst.positions


# ---------------------------------------------------------------------------
# Real broker-flat after a successful EXIT - the legitimate happy path.
# ---------------------------------------------------------------------------


def test_post_exit_first_response_flat_succeeds_fast(monkeypatch):
    """If the broker returns flat on the first poll and stays flat, the
    helper must not block for the full grace window - fast path.
    """
    inst, fc, state = _make_executor(monkeypatch)
    state["broker_book"].clear()  # broker is genuinely flat
    state["responses"] = ["flat"]

    inst._stamp_action("AAPL")
    inst._reconcile_position_with_broker("AAPL", expect="flat")

    # Single shot - no retry needed when broker's first answer matches expectation.
    assert len(state["calls"]) == 1
    assert "AAPL" not in inst.positions


def test_post_entry_first_response_present_succeeds_fast(monkeypatch):
    """If the broker has the position on the first poll, the helper
    accepts it immediately - no retry needed.
    """
    inst, fc, state = _make_executor(monkeypatch)
    state["broker_book"]["AAPL"] = {"qty": 18, "avg_entry_price": 270.00}

    inst._record_position("AAPL", "LONG", 9, 269.00)
    inst._reconcile_position_with_broker("AAPL", expect="present")

    # Single shot - broker's first answer matches the expectation.
    assert len(state["calls"]) == 1
    assert inst.positions["AAPL"]["qty"] == 18
    assert abs(inst.positions["AAPL"]["entry_price"] - 270.00) < 1e-6


# ---------------------------------------------------------------------------
# Stamp wiring - _record_position and _close_position_idempotent must mark
# the action timestamp so the helper knows the eventual-consistency window
# applies.
# ---------------------------------------------------------------------------


def test_record_position_stamps_action_ts(monkeypatch):
    inst, _fc, _state = _make_executor(monkeypatch)
    assert "AAPL" not in inst._last_action_ts
    inst._record_position("AAPL", "LONG", 10, 270.00)
    assert "AAPL" in inst._last_action_ts
    assert inst._within_action_grace("AAPL")


def test_close_position_idempotent_stamps_action_ts(monkeypatch):
    inst, fc, state = _make_executor(monkeypatch)
    state["broker_book"]["AAPL"] = {"qty": 10, "avg_entry_price": 270.00}
    inst._record_position("AAPL", "LONG", 10, 270.00)

    # Reset timestamp so we can prove _close_position_idempotent re-stamps.
    inst._last_action_ts.clear()
    inst._close_position_idempotent(fc, "AAPL", "TestStub", "STOP")

    assert "AAPL" in inst._last_action_ts
    assert inst._within_action_grace("AAPL")


def test_within_action_grace_expires(monkeypatch):
    inst, _fc, _state = _make_executor(monkeypatch)
    from executors import base as base_mod

    monkeypatch.setattr(base_mod, "RECONCILE_GRACE_SECONDS", 0.0)
    inst._stamp_action("AAPL")
    # Grace is 0 - timestamp is immediately stale.
    assert not inst._within_action_grace("AAPL")
    # Unstamped tickers are never in grace.
    assert not inst._within_action_grace("NEVER_TOUCHED")
