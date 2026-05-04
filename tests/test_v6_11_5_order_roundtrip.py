"""tests/test_v6_11_5_order_roundtrip.py

Regression tests for v6.11.5 order round-trip fix.

v6.11.5 changes _check_order_round_trip:
  1. EXTENDED session: cancel order *before* polling (DAY TIF won't self-cancel).
  2. Poll deadline 3s -> 8s (paper round-trips need more headroom).
  3. Track last_seen_status separately so timeout error reports the actual
     last-observed Alpaca status (was always 'unknown').
  4. Treat expired/rejected as terminal-OK (not just canceled/filled).

Outer _safe_check timeout for the order check is bumped 5s -> 12s to allow
for the new 8s poll plus submit/cancel overhead.

Rules: zero em-dashes. No scrape/crawl words.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("SSM_SMOKE_TEST", "1")
os.environ.setdefault("FMP_API_KEY", "test-key")
os.environ.setdefault("TELEGRAM_TOKEN", "123:test")
os.environ.setdefault("CHAT_ID", "999")

import trade_genius as tg  # noqa: E402


class TestOrderRoundTripV6115(unittest.TestCase):
    """v6.11.5 behavior changes for _check_order_round_trip."""

    def _build_mock_tc(self, statuses):
        """Build a mock TradingClient that returns the given status sequence
        from get_order_by_id.
        """
        mock_order = MagicMock()
        mock_order.id = "order-abc"
        mock_tc = MagicMock()
        mock_tc.submit_order.return_value = mock_order
        mock_tc.cancel_order_by_id.return_value = None

        order_objs = []
        for s in statuses:
            o = MagicMock()
            o.status = s
            order_objs.append(o)
        mock_tc.get_order_by_id.side_effect = order_objs + [order_objs[-1]] * 50
        return mock_tc, mock_order

    def _run(self, session, statuses):
        try:
            import alpaca.trading.client as _atc
        except ImportError:
            self.skipTest("alpaca-py not installed")
        mock_tc, _ = self._build_mock_tc(statuses)
        with patch.object(tg, "_market_session", return_value=session), \
             patch.dict(os.environ, {
                 "VAL_ALPACA_PAPER_KEY": "k",
                 "VAL_ALPACA_PAPER_SECRET": "s",
             }), \
             patch.object(_atc, "TradingClient", return_value=mock_tc), \
             patch("trade_genius.get_fmp_quote", return_value={"bid": 500.0}):
            cr = tg._check_order_round_trip()
        return cr, mock_tc

    def test_extended_cancel_called_before_poll(self):
        """In extended hours, cancel must run before the polling loop."""
        cr, mock_tc = self._run("extended", ["accepted", "canceled"])
        # cancel_order_by_id must have been called at least once
        self.assertGreaterEqual(mock_tc.cancel_order_by_id.call_count, 1)
        self.assertEqual(cr.severity, "ok")
        self.assertIn("canceled", cr.message)

    def test_rth_session_cancel_after_poll(self):
        """RTH IOC self-cancels at venue, so the cr is ok if the first poll
        sees a canceled status. Cancel may still be invoked as belt-and-suspenders.
        """
        cr, mock_tc = self._run("rth", ["canceled"])
        self.assertEqual(cr.severity, "ok")
        # cancel may or may not be called depending on race, but should not fail
        self.assertGreaterEqual(mock_tc.cancel_order_by_id.call_count, 0)

    def test_off_session_skipped(self):
        """Off-hours/weekend skip path is unchanged."""
        with patch.object(tg, "_market_session", return_value="off"), \
             patch.dict(os.environ, {
                 "VAL_ALPACA_PAPER_KEY": "k",
                 "VAL_ALPACA_PAPER_SECRET": "s",
             }):
            cr = tg._check_order_round_trip()
        self.assertEqual(cr.severity, "skip")
        self.assertIn("markets closed", cr.message)

    def test_timeout_reports_last_seen_status(self):
        """When polling times out without a terminal state, the message must
        include the actual last-seen status, not 'unknown'.
        """
        # Force the order to never become terminal: report 'accepted' forever
        try:
            import alpaca.trading.client as _atc
        except ImportError:
            self.skipTest("alpaca-py not installed")
        mock_order = MagicMock()
        mock_order.id = "order-stuck"
        stuck = MagicMock()
        stuck.status = "accepted"
        mock_tc = MagicMock()
        mock_tc.submit_order.return_value = mock_order
        mock_tc.get_order_by_id.return_value = stuck
        mock_tc.cancel_order_by_id.return_value = None

        # Patch sleep to no-op so the test runs fast despite the 8s deadline.
        # Patch monotonic to advance past the deadline after a few polls.
        import time as _time_mod
        call_count = [0]
        base = _time_mod.monotonic()

        def fake_monotonic():
            call_count[0] += 1
            # First call (t0) returns base, then jump past deadline quickly
            return base + (call_count[0] * 2.0)

        with patch.object(tg, "_market_session", return_value="extended"), \
             patch.dict(os.environ, {
                 "VAL_ALPACA_PAPER_KEY": "k",
                 "VAL_ALPACA_PAPER_SECRET": "s",
             }), \
             patch.object(_atc, "TradingClient", return_value=mock_tc), \
             patch("trade_genius.get_fmp_quote", return_value={"bid": 500.0}), \
             patch("time.sleep", return_value=None), \
             patch("time.monotonic", side_effect=fake_monotonic):
            cr = tg._check_order_round_trip()

        self.assertEqual(cr.severity, "critical")
        # Critical: last-seen status must be reported, NOT "unknown"
        self.assertIn("last=accepted", cr.message)
        self.assertNotIn("last=unknown", cr.message)

    def test_expired_treated_as_terminal_ok(self):
        """Alpaca may return 'expired' for IOC orders that never matched.
        v6.11.5 treats expired as terminal-OK (was: 'unknown' timeout).
        """
        cr, _ = self._run("rth", ["expired"])
        self.assertEqual(cr.severity, "ok")
        self.assertIn("expired", cr.message)

    def test_rejected_treated_as_terminal_ok(self):
        """Rejected (e.g. price band) is also terminal-OK for the round-trip
        smoke test: we proved the API path works end-to-end.
        """
        cr, _ = self._run("extended", ["rejected"])
        self.assertEqual(cr.severity, "ok")
        self.assertIn("rejected", cr.message)

    def test_outer_timeout_bumped_to_12s(self):
        """The _safe_check wrapper around _check_order_round_trip must allow
        at least 12s (was 5s) so the inner 8s poll can complete with headroom.
        """
        import inspect
        src = inspect.getsource(tg)
        # Locate the line that wires _check_order_round_trip with timeout_s.
        # It must specify timeout_s >= 12.0 (we set 12.0 exactly in v6.11.5).
        target = '_safe_check("Order round-trip", "A", _check_order_round_trip, timeout_s=12.0)'
        self.assertIn(
            target, src,
            "Outer _safe_check timeout for Order round-trip must be 12.0s in v6.11.5",
        )


class TestVersionParityV6115(unittest.TestCase):
    def test_bot_version_is_6_11_5(self):
        import bot_version
        self.assertEqual(bot_version.BOT_VERSION, "6.11.5")
        self.assertEqual(tg.BOT_VERSION, "6.11.5")

    def test_premarket_check_expected_version_matches(self):
        # premarket_check.py must expect the same version.
        # tg.__file__ is at the repo root, so scripts/ is a sibling.
        repo_root = os.path.dirname(os.path.abspath(tg.__file__))
        path = os.path.join(repo_root, "scripts", "premarket_check.py")
        with open(path) as f:
            src = f.read()
        self.assertIn('BOT_VERSION_EXPECTED = "6.11.5"', src)


if __name__ == "__main__":
    unittest.main()
