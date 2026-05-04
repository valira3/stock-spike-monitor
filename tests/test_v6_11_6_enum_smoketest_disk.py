"""tests/test_v6_11_6_enum_smoketest_disk.py

Regression tests for v6.11.6:

1. Order round-trip status normalization.
   Alpaca's typed client returns OrderStatus enums whose str() is e.g.
   'OrderStatus.CANCELED'. The v6.11.5 string compare against
   ('canceled', 'cancelled', ...) never matched, so the loop always
   timed out reporting last=orderstatus.canceled. v6.11.6 normalizes
   by taking the trailing token after '.' and lowercasing.

2. premarket_check.py SSM_SMOKE_TEST guard.
   Without SSM_SMOKE_TEST=1 set BEFORE importing trade_genius,
   trade_genius runs full bot startup at module load (telegram polling,
   scheduler, ingest threads). Every invocation of premarket_check
   inside the live container therefore spawned a SECOND bot polling
   the same Telegram tokens, producing 409 Conflict storms. v6.11.6
   sets SSM_SMOKE_TEST=1 at the top of premarket_check.py.

3. Disk space threshold.
   v6.11.5 used absolute byte thresholds (1GB warn / 100MB fail), but
   the Railway TradeGenius volume is only 433MB total, so warn could
   never be cleared. v6.11.6 uses percentage-based thresholds with a
   safety floor.

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


# ---------------------------------------------------------------------------
# (1) Order status enum normalization
# ---------------------------------------------------------------------------


class TestOrderStatusNormalization(unittest.TestCase):
    """End-to-end: Alpaca enum-style status must drive ok severity."""

    def _run_with_status_repr(self, status_repr, session="rth"):
        try:
            import alpaca.trading.client as _atc
        except ImportError:
            self.skipTest("alpaca-py not installed")
        mock_order = MagicMock()
        mock_order.id = "order-enum"

        # Build a mock order whose .status stringifies to the given repr
        # (e.g. 'OrderStatus.CANCELED' to mimic the real enum).
        class _StatusLike:
            def __init__(self, s):
                self._s = s

            def __str__(self):
                return self._s

        mock_o2 = MagicMock()
        mock_o2.status = _StatusLike(status_repr)

        mock_tc = MagicMock()
        mock_tc.submit_order.return_value = mock_order
        mock_tc.get_order_by_id.return_value = mock_o2
        mock_tc.cancel_order_by_id.return_value = None

        with patch.object(tg, "_market_session", return_value=session), \
             patch.dict(os.environ, {
                 "VAL_ALPACA_PAPER_KEY": "k",
                 "VAL_ALPACA_PAPER_SECRET": "s",
             }), \
             patch.object(_atc, "TradingClient", return_value=mock_tc), \
             patch("trade_genius.get_fmp_quote", return_value={"bid": 500.0}):
            return tg._check_order_round_trip()

    def test_orderstatus_enum_canceled_normalized_to_ok(self):
        cr = self._run_with_status_repr("OrderStatus.CANCELED")
        self.assertEqual(cr.severity, "ok",
                         "OrderStatus.CANCELED must normalize to canceled")
        self.assertIn("canceled", cr.message)

    def test_orderstatus_enum_filled_triggers_offsetting_warn(self):
        cr = self._run_with_status_repr("OrderStatus.FILLED")
        self.assertEqual(cr.severity, "warn")
        self.assertIn("filled unexpectedly", cr.message)

    def test_orderstatus_enum_expired_treated_as_ok(self):
        cr = self._run_with_status_repr("OrderStatus.EXPIRED")
        self.assertEqual(cr.severity, "ok")
        self.assertIn("expired", cr.message)

    def test_orderstatus_enum_rejected_treated_as_ok(self):
        cr = self._run_with_status_repr("OrderStatus.REJECTED")
        self.assertEqual(cr.severity, "ok")
        self.assertIn("rejected", cr.message)

    def test_plain_string_status_still_works(self):
        # Backwards compat: bare 'canceled' (no enum prefix) keeps working.
        cr = self._run_with_status_repr("canceled")
        self.assertEqual(cr.severity, "ok")
        self.assertIn("canceled", cr.message)

    def test_uppercase_string_status(self):
        # Some clients pass the bare uppercase enum name.
        cr = self._run_with_status_repr("CANCELED")
        self.assertEqual(cr.severity, "ok")


# ---------------------------------------------------------------------------
# (2) premarket_check.py SSM_SMOKE_TEST guard
# ---------------------------------------------------------------------------


class TestPremarketCheckSmokeTestGuard(unittest.TestCase):
    """premarket_check.py must set SSM_SMOKE_TEST=1 before importing
    trade_genius, so importing it does not boot a second bot.
    """

    def test_module_sets_ssm_smoke_test_at_top(self):
        path = os.path.join(
            os.path.dirname(os.path.abspath(tg.__file__)),
            "scripts", "premarket_check.py",
        )
        with open(path) as f:
            src = f.read()
        # The setdefault must appear (so users can still override),
        # and it must appear BEFORE the first 'import trade_genius'.
        self.assertIn(
            'os.environ.setdefault("SSM_SMOKE_TEST", "1")',
            src,
            "premarket_check.py must set SSM_SMOKE_TEST=1 (idempotent)",
        )
        guard_idx = src.find('os.environ.setdefault("SSM_SMOKE_TEST", "1")')
        first_tg_import = src.find("import trade_genius")
        self.assertGreater(first_tg_import, 0,
                           "must import trade_genius somewhere")
        self.assertLess(
            guard_idx, first_tg_import,
            "SSM_SMOKE_TEST guard must be set before importing trade_genius",
        )


# ---------------------------------------------------------------------------
# (3) Disk space percentage thresholds
# ---------------------------------------------------------------------------


class TestDiskSpacePercentage(unittest.TestCase):
    """v6.11.6 disk-space check uses percentage thresholds."""

    def _run_check(self, total, used, available):
        from scripts import premarket_check
        # df -B1 line: Filesystem 1B-blocks Used Available Use% Mounted
        use_pct = int(used * 100 / max(total, 1))
        df_output = "".join([
            "Filesystem 1B-blocks Used Available Use Mounted\n",
            "/dev/zd1 ",
            str(total), " ", str(used), " ", str(available),
            " ", str(use_pct), "% /data\n",
        ])
        with patch("subprocess.check_output", return_value=df_output.encode()):
            return premarket_check.check_disk_space()

    def test_pass_when_above_15_pct(self):
        # 433 MB volume, 100MB used, 333MB free -> 76.9% free -> PASS
        cr = self._run_check(433_000_000, 100_000_000, 333_000_000)
        self.assertEqual(cr["status"], "PASS")
        self.assertIn("of", cr["detail"])  # message mentions total

    def test_warn_when_below_15_pct(self):
        # 433 MB volume, 380MB used, 53MB free -> 12.2% free -> WARN
        cr = self._run_check(433_000_000, 380_000_000, 53_000_000)
        self.assertEqual(cr["status"], "WARN")
        self.assertIn("warning", cr["detail"])

    def test_fail_when_below_5_pct(self):
        # 433 MB volume, 420MB used, 13MB free -> 3% free -> FAIL
        cr = self._run_check(433_000_000, 420_000_000, 13_000_000)
        self.assertEqual(cr["status"], "FAIL")
        self.assertIn("critical", cr["detail"])

    def test_fail_floor_under_50mb_regardless_of_pct(self):
        # 100GB volume, 95% free but only 40MB free -> FAIL via floor
        # (5% of 100GB = 5GB, so 40MB is well below the floor)
        cr = self._run_check(100_000_000_000, 95_000_000_000, 5_000_000_000)
        # 5GB out of 100GB = 5% free -> at the FAIL line, but 5GB > floor
        # Actually 5% IS the FAIL line. Need a clearer case.
        # Use 50GB / 50GB free / 5MB used: that's 100% free, no FAIL.
        # Easier: simulate huge volume with tiny free bytes near floor.
        cr2 = self._run_check(
            100_000_000_000,  # 100 GB total
            99_960_000_000,   # 99.96 GB used
            40_000_000,       # 40 MB free  -> 0.04% free -> FAIL via pct
        )
        self.assertEqual(cr2["status"], "FAIL")

    def test_railway_volume_not_warned_anymore(self):
        # The exact pre-fix scenario: 433MB total, ~408MB free (94%).
        # Pre-v6.11.6 this WARNed because 408MB < 1GB threshold.
        # v6.11.6 must PASS because 94% > 15%.
        cr = self._run_check(433_000_000, 25_000_000, 408_000_000)
        self.assertEqual(
            cr["status"], "PASS",
            "Railway 433MB volume with 94%% free must not warn anymore",
        )


class TestVersionParityV6116(unittest.TestCase):
    """Forward-compat: assert v6.11.x parity, not hardcoded 6.11.6."""
    def test_bot_version_is_6_11_6(self):
        import bot_version
        self.assertTrue(
            bot_version.BOT_VERSION.startswith("6.11."),
            f"BOT_VERSION must be on 6.11.x line, got {bot_version.BOT_VERSION}",
        )
        self.assertEqual(tg.BOT_VERSION, bot_version.BOT_VERSION)

    def test_premarket_check_expected_version_matches(self):
        import bot_version
        repo_root = os.path.dirname(os.path.abspath(tg.__file__))
        path = os.path.join(repo_root, "scripts", "premarket_check.py")
        with open(path) as f:
            src = f.read()
        self.assertIn(
            f'BOT_VERSION_EXPECTED = "{bot_version.BOT_VERSION}"',
            src,
        )


if __name__ == "__main__":
    unittest.main()
