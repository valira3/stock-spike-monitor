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
        """Stand-in for trade_genius._to_et_hhmm.

        v8.3.5 behavior: tz-aware UTC ISO -> ET; tz-naive "HH:MM:SS" or
        pre-formatted strings pass through (after we lop off seconds).
        """
        if not iso:
            return ""
        if "T" in iso and ("Z" in iso or "+" in iso):
            # Treat as UTC ISO; convert to ET (UTC-4 during EDT on 2026-05-12)
            try:
                from datetime import datetime, timezone, timedelta

                s = iso.replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                et_dt = dt.astimezone(timezone(timedelta(hours=-4)))
                return et_dt.strftime("%H:%M ET")
            except Exception:
                return iso
        if ":" in iso:
            return iso[:5] + " ET"
        return iso

    fake._to_et_hhmm = _to_et_hhmm

    monkeypatch.setattr(dashboard_server, "_ssm", lambda: fake)
    # v9.1.69 -- _today_trades now appends EOD trade rows from
    # _eod_trade_rows_for_pid("main"), which reads /data/eod_trade_log.jsonl.
    # That file may exist locally from a real session. Stub it out so these
    # tests remain isolated from filesystem state.
    monkeypatch.setattr(dashboard_server, "_eod_trade_rows_for_pid", lambda _pid: [])
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
        fake.trade_log_read_tail = lambda **_kw: [
            {
                "date": today,
                "portfolio": "paper",
                "ticker": "AAPL",
                "side": "LONG",
                "shares": 10,
                "entry_price": 150.0,
                "exit_price": 152.5,
                "entry_time": "10:00 ET",
                "exit_time": "10:30 ET",
                "pnl": 25.0,
                "pnl_pct": 1.67,
                "reason": "target",
            }
        ]
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
        fake.trade_log_read_tail = lambda **_kw: [
            {
                "date": today,
                "portfolio": "paper",
                "ticker": "NVDA",
                "side": "SHORT",
                "shares": 5,
                "entry_price": 220.0,
                "exit_price": 215.0,
                "entry_time": "09:50 ET",
                "exit_time": "10:15 ET",
                "pnl": 25.0,
                "pnl_pct": 2.27,
                "reason": "target",
            }
        ]
        out = dashboard_server._today_trades()
        actions = [(r["action"], r["ticker"], r["side"]) for r in out]
        assert ("SHORT", "NVDA", "SHORT") in actions
        assert ("COVER", "NVDA", "SHORT") in actions

    def test_inmem_paper_trade_takes_precedence_no_duplicate(self, fake_ssm):
        """When paper_trades already has the BUY row (same ticker/time/side),
        the trade_log-synthesized BUY should NOT be appended again."""
        fake, today = fake_ssm
        fake.paper_trades = [
            {
                "action": "BUY",
                "ticker": "AAPL",
                "price": 150.0,
                "shares": 10,
                "time": "10:00 ET",
                "date": today,
                "side": "LONG",
                "entry_num": 1,
                "cost": 1500.0,
            }
        ]
        fake.trade_log_read_tail = lambda **_kw: [
            {
                "date": today,
                "portfolio": "paper",
                "ticker": "AAPL",
                "side": "LONG",
                "shares": 10,
                "entry_price": 150.0,
                "exit_price": 152.5,
                "entry_time": "10:00 ET",
                "exit_time": "10:30 ET",
                "pnl": 25.0,
                "pnl_pct": 1.67,
                "reason": "target",
            }
        ]
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
        fake.trade_log_read_tail = lambda **_kw: [
            {
                "date": "2026-05-11",  # yesterday
                "portfolio": "paper",
                "ticker": "AAPL",
                "side": "LONG",
                "shares": 1,
                "entry_price": 100.0,
                "exit_price": 101.0,
                "entry_time": "10:00 ET",
                "exit_time": "10:30 ET",
                "pnl": 1.0,
                "pnl_pct": 1.0,
                "reason": "target",
            }
        ]
        out = dashboard_server._today_trades()
        # Yesterday's row must NOT leak into today's view
        aapl = [r for r in out if r["ticker"] == "AAPL"]
        assert aapl == []

    def test_log_read_failure_doesnt_break_inmem_path(self, fake_ssm):
        """If trade_log_read_tail raises, the in-memory paper_trades
        path must still surface."""
        fake, today = fake_ssm
        fake.paper_trades = [
            {
                "action": "BUY",
                "ticker": "AAPL",
                "price": 150.0,
                "shares": 10,
                "time": "10:00 ET",
                "date": today,
                "side": "LONG",
            }
        ]

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
        fake.trade_log_read_tail = lambda **_kw: [
            {
                "date": today,
                "portfolio": "paper",
                "ticker": "TSLA",
                "side": "LONG",
                "shares": 7,
                "entry_price": 200.0,
                "exit_price": 195.0,
                "entry_time": "09:35 ET",
                "exit_time": "11:42 ET",
                "pnl": -35.0,
                "pnl_pct": -2.5,
                "reason": "stop_atr",
            }
        ]
        out = dashboard_server._today_trades()
        sell = next(r for r in out if r["action"] == "SELL")
        assert sell["pnl"] == -35.0
        assert sell["pnl_pct"] == -2.5
        assert sell["reason"] == "stop_atr"
        assert sell["entry_price"] == 200.0
        assert sell["exit_price"] == 195.0

    def test_utc_iso_exit_time_converted_to_et(self, fake_ssm):
        """v8.3.5 -- broker/orders.py:2049 writes exit_time as a full
        UTC ISO ('2026-05-12T14:29:00Z'). Pre-v8.3.5 the synth row
        passed this raw to the dashboard, which sliced 'HH:MM' and
        rendered '14:29' instead of the correct '10:29 ET'. The
        synth path now routes through _to_et_hhmm.
        """
        fake, today = fake_ssm
        fake.trade_log_read_tail = lambda **_kw: [
            {
                "date": today,
                "portfolio": "paper",
                "ticker": "NFLX",
                "side": "LONG",
                "shares": 859,
                "entry_price": 87.59,
                "exit_price": 88.12,
                "entry_time": "10:16:42",
                "exit_time": "2026-05-12T14:29:00Z",  # raw UTC ISO
                "pnl": 450.80,
                "pnl_pct": 0.60,
                "reason": "target",
            }
        ]
        out = dashboard_server._today_trades()
        sell = next(r for r in out if r["action"] == "SELL")
        # The bug surfaced as '14:29' (sliced raw UTC). The fix shows '10:29 ET'.
        assert sell["time"] == "10:29 ET", f"expected ET conversion, got {sell['time']!r}"
        assert sell["exit_time"] == "10:29 ET"
        # Entry side: "10:16:42" -> "10:16 ET"
        buy = next(r for r in out if r["action"] == "BUY")
        assert buy["time"] == "10:16 ET"

    def test_cover_dedup_inmem_vs_log_same_close(self, fake_ssm):
        """v8.3.11 -- operator surfaced AMZN COVER rendering TWICE
        (one at 11:00 with entry-price $264.05, one at 11:14 with
        correct exit-price $265.12). Root cause: in-memory
        short_trade_history COVER has no "time" field (history_record
        shape), so pre-v8.3.11 _key() fell back to entry_time.
        v8.3.3 synth_exit set "time" to exit_time, so its key fell
        back to "time". Different keys -> no dedup -> double render.

        After v8.3.11, both paths produce the same dedup key
        keyed on exit_time for close actions. The same close
        renders once.
        """
        fake, today = fake_ssm
        # In-memory COVER from short_trade_history (history_record
        # shape: NO "time" field, has entry_time + exit_time).
        fake.short_trade_history = [
            {
                "ticker": "AMZN",
                "side": "SHORT",
                "action": "COVER",
                "shares": 58,
                "entry_price": 264.05,
                "exit_price": 265.12,
                "pnl": -62.13,
                "pnl_pct": -0.41,
                "entry_time": "11:00 ET",
                "exit_time": "11:14 ET",
                "date": today,
            }
        ]
        # Same close present on disk in trade_log.jsonl with UTC ISO
        # exit_time (will be normalized through _to_et_hhmm).
        fake.trade_log_read_tail = lambda **_kw: [
            {
                "date": today,
                "portfolio": "paper",
                "ticker": "AMZN",
                "side": "SHORT",
                "shares": 58,
                "entry_price": 264.05,
                "exit_price": 265.12,
                "entry_time": "11:00:00",
                "exit_time": "2026-05-12T15:14:00Z",  # UTC -> 11:14 ET
                "pnl": -62.13,
                "pnl_pct": -0.41,
                "reason": "stop_atr",
            }
        ]
        out = dashboard_server._today_trades()
        # Filter to COVER rows
        cover_rows = [r for r in out if r["action"] == "COVER"]
        # Exactly one COVER row should render
        assert len(cover_rows) == 1, f"expected 1 COVER, got {len(cover_rows)}: {cover_rows!r}"

    def test_sort_order_entry_before_exit(self, fake_ssm):
        """In the output, entry row sorts before exit row (entry_time <
        exit_time lexically when both ET-formatted as HH:MM)."""
        fake, today = fake_ssm
        fake.trade_log_read_tail = lambda **_kw: [
            {
                "date": today,
                "portfolio": "paper",
                "ticker": "MSFT",
                "side": "LONG",
                "shares": 3,
                "entry_price": 400.0,
                "exit_price": 405.0,
                "entry_time": "09:35 ET",
                "exit_time": "11:42 ET",
                "pnl": 15.0,
                "pnl_pct": 1.25,
                "reason": "target",
            }
        ]
        out = dashboard_server._today_trades()
        buy_idx = next(i for i, r in enumerate(out) if r["action"] == "BUY")
        sell_idx = next(i for i, r in enumerate(out) if r["action"] == "SELL")
        assert buy_idx < sell_idx
