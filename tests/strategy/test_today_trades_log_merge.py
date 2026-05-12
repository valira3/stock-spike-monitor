"""v8.3.3 -- dashboard_server._today_trades trade-log merge tests.

Root cause covered: paper_state.json (rehydrates paper_trades on boot)
is saved every 5 minutes; trade_log.jsonl is appended synchronously per
trade. After a Railway redeploy, the latest 0-5 min of trades exist on
disk in trade_log.jsonl but are missing from in-memory paper_trades /
short_trade_history. v8.3.3 has _today_trades() backfill from the log
so the dashboard shows the complete picture.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import dashboard_server


@pytest.fixture
def fake_ssm(monkeypatch):
    """Patch dashboard_server._ssm to return a configurable fake module."""
    today = "2026-05-12"

    fake = SimpleNamespace()
    fake.paper_trades = []
    fake.short_trade_history = []
    fake.short_positions = {}
    fake.trade_log_read_tail = lambda **_kw: []

    def _now_et():
        # datetime-like with strftime
        class _Stub:
            def strftime(self, fmt):
                if fmt == "%Y-%m-%d":
                    return today
                return ""
        return _Stub()
    fake._now_et = _now_et

    def _to_et_hhmm(iso):
        # Simplistic stub: extract HH:MM out of iso if present.
        if not iso:
            return ""
        if "T" in iso:
            return iso.split("T")[1][:5] + " ET"
        return iso[:5] + " ET"
    fake._to_et_hhmm = _to_et_hhmm

    monkeypatch.setattr(dashboard_server, "_ssm", lambda: fake)
    return fake, today


class TestTodayTradesLogMerge:

    def test_empty_state_returns_empty(self, fake_ssm):
        fake, today = fake_ssm
        out = dashboard_server._today_trades()
        assert out == []

    def test_log_only_long_round_trip_synthesized(self, fake_ssm):
        """paper_trades empty (lost on redeploy) but trade_log.jsonl
        has today's LONG round-trip. Both BUY + SELL rows should
        appear in the output."""
        fake, today = fake_ssm
        fake.trade_log_read_tail = lambda **_kw: [{
            "date": today, "portfolio": "paper",
            "ticker": "AAPL", "side": "LONG", "shares": 10,
            "entry_price": 150.0, "exit_price": 152.5,
            "entry_time": "10:00 ET", "exit_time": "10:30 ET",
            "pnl": 25.0, "pnl_pct": 1.67, "reason": "target",
        }]
        out = dashboard_server._today_trades()
        actions = sorted([(r["action"], r["ticker"]) for r in out])
        assert actions == [("BUY", "AAPL"), ("SELL", "AAPL")]
        sell_row = next(r for r in out if r["action"] == "SELL")
        assert sell_row["pnl"] == 25.0
        assert sell_row["entry_price"] == 150.0
        assert sell_row["exit_price"] == 152.5
        buy_row = next(r for r in out if r["action"] == "BUY")
        assert buy_row["price"] == 150.0
        assert buy_row["shares"] == 10

    def test_log_short_round_trip_synthesized(self, fake_ssm):
        fake, today = fake_ssm
        fake.trade_log_read_tail = lambda **_kw: [{
            "date": today, "portfolio": "paper",
            "ticker": "NVDA", "side": "SHORT", "shares": 5,
            "entry_price": 220.0, "exit_price": 215.0,
            "entry_time": "09:50 ET", "exit_time": "10:15 ET",
            "pnl": 25.0, "pnl_pct": 2.27, "reason": "target",
        }]
        out = dashboard_server._today_trades()
        actions = [(r["action"], r["ticker"], r["side"]) for r in out]
        assert ("SHORT", "NVDA", "SHORT") in actions
        assert ("COVER", "NVDA", "SHORT") in actions

    def test_inmem_paper_trade_takes_precedence_no_duplicate(self, fake_ssm):
        """When paper_trades already has the BUY row (same ticker/time/side),
        the trade_log-synthesized BUY should NOT be appended again."""
        fake, today = fake_ssm
        fake.paper_trades = [{
            "action": "BUY", "ticker": "AAPL", "price": 150.0,
            "shares": 10, "time": "10:00 ET", "date": today,
            "side": "LONG", "entry_num": 1, "cost": 1500.0,
        }]
        fake.trade_log_read_tail = lambda **_kw: [{
            "date": today, "portfolio": "paper",
            "ticker": "AAPL", "side": "LONG", "shares": 10,
            "entry_price": 150.0, "exit_price": 152.5,
            "entry_time": "10:00 ET", "exit_time": "10:30 ET",
            "pnl": 25.0, "pnl_pct": 1.67, "reason": "target",
        }]
        out = dashboard_server._today_trades()
        buys = [r for r in out if r["action"] == "BUY" and r["ticker"] == "AAPL"]
        assert len(buys) == 1  # No duplicate from log
        # SELL should be added from the log (not in paper_trades yet)
        sells = [r for r in out if r["action"] == "SELL" and r["ticker"] == "AAPL"]
        assert len(sells) == 1
        assert sells[0]["pnl"] == 25.0

    def test_log_rows_filtered_to_today_only(self, fake_ssm):
        fake, today = fake_ssm
        # Note: trade_log_read_tail receives a since_date filter; the
        # safety check in _today_trades also re-filters to make
        # absolutely sure stale rows can't sneak in.
        fake.trade_log_read_tail = lambda **_kw: [{
            "date": "2026-05-11",  # yesterday
            "portfolio": "paper", "ticker": "AAPL", "side": "LONG",
            "shares": 1, "entry_price": 100.0, "exit_price": 101.0,
            "entry_time": "10:00 ET", "exit_time": "10:30 ET",
            "pnl": 1.0, "pnl_pct": 1.0, "reason": "target",
        }]
        out = dashboard_server._today_trades()
        # Yesterday's row must NOT leak into today's view
        aapl = [r for r in out if r["ticker"] == "AAPL"]
        assert aapl == []

    def test_log_read_failure_doesnt_break_inmem_path(self, fake_ssm):
        """If trade_log_read_tail raises, the in-memory paper_trades
        path must still surface."""
        fake, today = fake_ssm
        fake.paper_trades = [{
            "action": "BUY", "ticker": "AAPL", "price": 150.0,
            "shares": 10, "time": "10:00 ET", "date": today,
            "side": "LONG",
        }]
        def _boom(**_kw):
            raise RuntimeError("disk read failed")
        fake.trade_log_read_tail = _boom
        out = dashboard_server._today_trades()
        assert len(out) >= 1
        assert out[0]["ticker"] == "AAPL"

    def test_pnl_fields_preserved_on_synth_sell(self, fake_ssm):
        """The synthesized SELL row carries pnl, pnl_pct, reason, and
        the entry_price for proper rendering on Main."""
        fake, today = fake_ssm
        fake.trade_log_read_tail = lambda **_kw: [{
            "date": today, "portfolio": "paper",
            "ticker": "TSLA", "side": "LONG", "shares": 7,
            "entry_price": 200.0, "exit_price": 195.0,
            "entry_time": "09:35 ET", "exit_time": "11:42 ET",
            "pnl": -35.0, "pnl_pct": -2.5, "reason": "stop_atr",
        }]
        out = dashboard_server._today_trades()
        sell = next(r for r in out if r["action"] == "SELL")
        assert sell["pnl"] == -35.0
        assert sell["pnl_pct"] == -2.5
        assert sell["reason"] == "stop_atr"
        assert sell["entry_price"] == 200.0
        assert sell["exit_price"] == 195.0

    def test_sort_order_entry_before_exit(self, fake_ssm):
        """In the output, entry row sorts before exit row (entry_time <
        exit_time lexically when both ET-formatted as HH:MM)."""
        fake, today = fake_ssm
        fake.trade_log_read_tail = lambda **_kw: [{
            "date": today, "portfolio": "paper",
            "ticker": "MSFT", "side": "LONG", "shares": 3,
            "entry_price": 400.0, "exit_price": 405.0,
            "entry_time": "09:35 ET", "exit_time": "11:42 ET",
            "pnl": 15.0, "pnl_pct": 1.25, "reason": "target",
        }]
        out = dashboard_server._today_trades()
        buy_idx = next(i for i, r in enumerate(out) if r["action"] == "BUY")
        sell_idx = next(i for i, r in enumerate(out) if r["action"] == "SELL")
        assert buy_idx < sell_idx
