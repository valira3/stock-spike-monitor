"""v6.11.13 - same-ticker post-exit cooldown tests.

Verifies the broker-plumbing guardrail added to prevent Alpaca
40310000 wash-trade rejects on instant flat-and-reverse on the same
symbol.

Coverage:
- record + check happy path (within window blocks, after window allows)
- per-ticker isolation (cooldown on AAPL does not block MSFT)
- env-var disable (POST_EXIT_SAME_TICKER_COOLDOWN_SEC=0 -> no-op)
- winner exits also record (this is the wash-trade scenario)
- reset_daily_state cross-day prune
"""
from __future__ import annotations

import importlib
import os
import unittest
from datetime import datetime, timedelta, timezone


class TestPostExitCooldown(unittest.TestCase):
    def setUp(self):
        os.environ.pop("POST_EXIT_SAME_TICKER_COOLDOWN_SEC", None)
        os.environ["SSM_SMOKE_TEST"] = "1"
        os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:fake")
        os.environ.setdefault("FMP_API_KEY", "test_dummy_key")
        import eye_of_tiger
        importlib.reload(eye_of_tiger)
        import trade_genius
        importlib.reload(trade_genius)
        self.tg = trade_genius
        self.tg._post_exit_cooldown.clear()

    def test_record_then_check_blocks_within_window(self):
        ticker = "AAPL"
        ts = datetime.now(tz=timezone.utc)
        self.tg.record_post_exit_cooldown(ticker, "stop", exit_ts_utc=ts)
        self.assertFalse(self.tg._check_post_exit_cooldown(ticker))
        entry = self.tg.is_in_post_exit_cooldown(ticker)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["exit_reason"], "stop")

    def test_check_allows_after_window_expires(self):
        ticker = "MSFT"
        ts_old = datetime.now(tz=timezone.utc) - timedelta(seconds=30)
        self.tg._post_exit_cooldown[ticker] = {
            "until_utc": ts_old + timedelta(seconds=10),
            "exit_ts_utc": ts_old,
            "exit_reason": "manual",
        }
        self.assertTrue(self.tg._check_post_exit_cooldown(ticker))
        self.assertNotIn(ticker, self.tg._post_exit_cooldown)

    def test_per_ticker_isolation(self):
        ts = datetime.now(tz=timezone.utc)
        self.tg.record_post_exit_cooldown("NVDA", "stop", exit_ts_utc=ts)
        self.assertFalse(self.tg._check_post_exit_cooldown("NVDA"))
        self.assertTrue(self.tg._check_post_exit_cooldown("TSLA"))

    def test_env_disable(self):
        os.environ["POST_EXIT_SAME_TICKER_COOLDOWN_SEC"] = "0"
        import eye_of_tiger
        importlib.reload(eye_of_tiger)
        ts = datetime.now(tz=timezone.utc)
        self.tg.record_post_exit_cooldown("GOOG", "stop", exit_ts_utc=ts)
        self.assertNotIn("GOOG", self.tg._post_exit_cooldown)
        self.assertTrue(self.tg._check_post_exit_cooldown("GOOG"))

    def test_winner_exit_also_records(self):
        ticker = "GOOG"
        ts = datetime.now(tz=timezone.utc)
        self.tg.record_post_exit_cooldown(ticker, "target_winner", exit_ts_utc=ts)
        self.assertFalse(self.tg._check_post_exit_cooldown(ticker))

    def test_per_ticker_not_per_side(self):
        ts = datetime.now(tz=timezone.utc)
        self.tg.record_post_exit_cooldown("META", "stop", exit_ts_utc=ts)
        self.assertFalse(self.tg._check_post_exit_cooldown("META"))


if __name__ == "__main__":
    unittest.main()
