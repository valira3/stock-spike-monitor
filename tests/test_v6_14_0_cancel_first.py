"""v6.14.0 cancel-first entry guard tests (issue #357).

Covers ``broker.orders._cancel_first_guard``:

  1. Replay path / no creds: helper returns True without touching any
     broker client (no-op).
  2. No opposing orders on the ticker: returns True without issuing
     a cancel.
  3. Same-side open order on the ticker is left alone (a leftover
     working entry on the same side is not a wash-trade hazard).
  4. Opposing-side open order is cancelled and the helper polls
     ``get_order_by_id`` until the cancel ack arrives, then returns
     True.
  5. Opposing-side cancel that never acks (status stays ``new``)
     returns False after the configured timeout, so the caller skips
     the entry.
  6. Multiple opposing orders are all cancelled and the helper waits
     for ALL acks before returning.
  7. Broker raises on ``get_orders``: helper logs and fails open
     (returns True), preserving the v6.11.13 cooldown as the only
     guardrail rather than bricking the entry path.

NOTE: this test file is intentionally em-dash free per the v6.14.0
author guidelines.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _FakeSide:
    def __init__(self, value):
        self.value = value


class _FakeStatus:
    def __init__(self, value):
        self.value = value


class _FakeOrder:
    def __init__(self, oid, side, status="new"):
        self.id = oid
        self.side = _FakeSide(side)
        self.status = _FakeStatus(status)


def _make_client(open_orders, status_sequence=None):
    """Build a mock TradingClient.

    ``open_orders`` is the list returned by ``get_orders``.
    ``status_sequence`` is an optional dict mapping order id to a list
    of status strings ``get_order_by_id`` should yield in order. If
    not provided, every order id resolves to ``"canceled"`` on first
    poll.
    """
    client = MagicMock()
    client.get_orders.return_value = list(open_orders)

    seq = dict(status_sequence or {})

    def _by_id(oid):
        oid_s = str(oid)
        if oid_s in seq and seq[oid_s]:
            status = seq[oid_s].pop(0)
        else:
            status = "canceled"
        return _FakeOrder(oid_s, "sell", status=status)

    client.get_order_by_id.side_effect = _by_id
    client.cancel_order_by_id.return_value = None
    return client


class CancelFirstGuardTests(unittest.TestCase):
    def setUp(self):
        # Import inside setUp so each test gets a clean module ref.
        from broker import orders as _orders

        self.orders = _orders
        # Drop the polling sleep to keep the suite fast.
        self._orig_interval = _orders._CANCEL_FIRST_POLL_INTERVAL_MS
        _orders._CANCEL_FIRST_POLL_INTERVAL_MS = 1

    def tearDown(self):
        self.orders._CANCEL_FIRST_POLL_INTERVAL_MS = self._orig_interval

    def test_no_client_is_noop(self):
        """Replay path: helper returns True without raising."""
        result = self.orders._cancel_first_guard(
            "AAPL", "long", broker_client=None
        )
        # With no env creds set in the sandbox, builder also returns
        # None, so the helper short-circuits to True.
        self.assertTrue(result)

    def test_no_open_orders_passes(self):
        client = _make_client(open_orders=[])
        result = self.orders._cancel_first_guard(
            "AAPL", "long", broker_client=client
        )
        self.assertTrue(result)
        client.cancel_order_by_id.assert_not_called()

    def test_same_side_order_is_left_alone(self):
        """A leftover working LONG entry is not opposing for a new LONG."""
        client = _make_client(open_orders=[_FakeOrder("o-1", "buy")])
        result = self.orders._cancel_first_guard(
            "AAPL", "long", broker_client=client
        )
        self.assertTrue(result)
        client.cancel_order_by_id.assert_not_called()

    def test_opposing_order_is_cancelled_and_polled(self):
        """Working SELL on AAPL when entering LONG: cancel + wait for ack."""
        client = _make_client(
            open_orders=[_FakeOrder("o-99", "sell")],
            status_sequence={"o-99": ["new", "pending_cancel", "canceled"]},
        )
        result = self.orders._cancel_first_guard(
            "AAPL", "long", broker_client=client
        )
        self.assertTrue(result)
        client.cancel_order_by_id.assert_called_once_with("o-99")
        # Polled until terminal status appeared.
        self.assertGreaterEqual(client.get_order_by_id.call_count, 3)

    def test_cancel_ack_timeout_skips_entry(self):
        """If the cancel never acks, helper returns False so caller skips."""
        # Force a tight timeout so the test runs fast.
        import eye_of_tiger as eot

        original = eot.CANCEL_ACK_TIMEOUT_MS
        eot.CANCEL_ACK_TIMEOUT_MS = 50
        try:
            client = MagicMock()
            client.get_orders.return_value = [_FakeOrder("stuck-1", "buy")]
            client.cancel_order_by_id.return_value = None
            client.get_order_by_id.return_value = _FakeOrder(
                "stuck-1", "buy", status="new"
            )
            result = self.orders._cancel_first_guard(
                "MSFT", "short", broker_client=client
            )
        finally:
            eot.CANCEL_ACK_TIMEOUT_MS = original

        self.assertFalse(result)
        client.cancel_order_by_id.assert_called_once_with("stuck-1")

    def test_multiple_opposing_orders_all_cancelled(self):
        client = _make_client(
            open_orders=[
                _FakeOrder("a", "sell"),
                _FakeOrder("b", "sell"),
                _FakeOrder("c", "buy"),  # same-side as new LONG, ignored
            ],
        )
        result = self.orders._cancel_first_guard(
            "TSLA", "long", broker_client=client
        )
        self.assertTrue(result)
        cancelled = {
            call.args[0] for call in client.cancel_order_by_id.call_args_list
        }
        self.assertEqual(cancelled, {"a", "b"})

    def test_get_orders_failure_fails_open(self):
        """A broker hiccup must not brick the entry path."""
        client = MagicMock()
        client.get_orders.side_effect = RuntimeError("alpaca 503")
        result = self.orders._cancel_first_guard(
            "NVDA", "long", broker_client=client
        )
        self.assertTrue(result)
        client.cancel_order_by_id.assert_not_called()


if __name__ == "__main__":
    unittest.main()
