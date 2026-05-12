"""v8.3.7 -- _compute_realized_pnl_today tests.

Operator surfaced: NFLX closed earlier with +$450.80 realized, but
the dashboard's Day P&L showed -$241.56 (only the open-position
unrealized). Root cause: realized P&L was summed only from in-memory
paper_trades, which gets wiped on Railway redeploy and rehydrated
from the 5-minute paper_state.json save. The synchronous
trade_log.jsonl on disk has the closing rows but the realized sum
never read from there. v8.3.7 closes the same gap that v8.3.3 fixed
for the Today's Trades panel -- but on the P&L summation side.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from dashboard_server import _compute_realized_pnl_today


def _ssm_with(paper_trades=None, short_history=None, log_rows=None,
              fail_log_read=False):
    """Build a fake _ssm() return value with configurable trade
    sources + matching _to_et_hhmm helper."""
    fake = SimpleNamespace()
    fake.paper_trades = list(paper_trades or [])
    fake.short_trade_history = list(short_history or [])

    def _read_log(**_kw):
        if fail_log_read:
            raise RuntimeError("simulated disk read failure")
        return list(log_rows or [])
    fake.trade_log_read_tail = _read_log

    def _to_et_hhmm(iso):
        """Stub matching trade_genius._to_et_hhmm for tests."""
        if not iso:
            return ""
        if "T" in iso and ("Z" in iso or "+" in iso):
            try:
                from datetime import datetime, timezone, timedelta
                s = iso.replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                et = dt.astimezone(timezone(timedelta(hours=-4)))
                return et.strftime("%H:%M ET")
            except Exception:
                return iso
        if ":" in iso:
            return iso[:5] + " ET"
        return iso
    fake._to_et_hhmm = _to_et_hhmm
    return fake


TODAY = "2026-05-12"


class TestRealizedFromInMemoryOnly:

    def test_empty_returns_zero(self):
        m = _ssm_with()
        assert _compute_realized_pnl_today(m, TODAY) == 0.0

    def test_paper_trades_sell_row(self):
        m = _ssm_with(paper_trades=[
            {"action": "SELL", "ticker": "AAPL", "pnl": 450.80,
             "date": TODAY, "time": "10:29 ET"},
        ])
        assert _compute_realized_pnl_today(m, TODAY) == pytest.approx(450.80)

    def test_paper_trades_buy_row_ignored(self):
        """BUY rows don't carry pnl; must not be counted."""
        m = _ssm_with(paper_trades=[
            {"action": "BUY", "ticker": "AAPL", "pnl": 999.0,  # bogus
             "date": TODAY, "time": "10:00 ET"},
        ])
        assert _compute_realized_pnl_today(m, TODAY) == 0.0

    def test_short_trade_history_summed(self):
        m = _ssm_with(short_history=[
            {"ticker": "NVDA", "pnl": 25.0, "date": TODAY,
             "time": "10:15 ET"},
        ])
        assert _compute_realized_pnl_today(m, TODAY) == 25.0

    def test_other_day_ignored(self):
        m = _ssm_with(paper_trades=[
            {"action": "SELL", "ticker": "AAPL", "pnl": 999.0,
             "date": "2026-05-11", "time": "10:29 ET"},
        ])
        assert _compute_realized_pnl_today(m, TODAY) == 0.0


class TestRealizedFromTradeLog:

    def test_log_row_only_summed(self):
        """Operator's scenario: paper_trades empty (redeploy wiped it),
        but trade_log.jsonl has the close. Must surface the realized."""
        m = _ssm_with(log_rows=[{
            "date": TODAY, "portfolio": "paper",
            "ticker": "NFLX", "side": "LONG", "shares": 859,
            "entry_price": 87.59, "exit_price": 88.12,
            "entry_time": "10:16:42",
            "exit_time": "2026-05-12T14:29:00Z",
            "pnl": 450.80, "pnl_pct": 0.60, "reason": "target",
        }])
        assert _compute_realized_pnl_today(m, TODAY) == pytest.approx(450.80)

    def test_dedup_paper_trades_wins(self):
        """When the same close exists in BOTH paper_trades AND
        trade_log.jsonl, count it once."""
        m = _ssm_with(
            paper_trades=[{
                "action": "SELL", "ticker": "NFLX", "pnl": 450.80,
                "date": TODAY, "time": "10:29 ET",
            }],
            log_rows=[{
                "date": TODAY, "portfolio": "paper",
                "ticker": "NFLX", "side": "LONG", "shares": 859,
                "entry_price": 87.59, "exit_price": 88.12,
                "entry_time": "10:16:42",
                "exit_time": "2026-05-12T14:29:00Z",
                "pnl": 450.80, "pnl_pct": 0.60, "reason": "target",
            }],
        )
        # Single count, not 901.60
        assert _compute_realized_pnl_today(m, TODAY) == pytest.approx(450.80)

    def test_full_operator_scenario(self):
        """Two closed trades today summing to +$780 realized; the
        operator's exact mismatch (Day P&L showed only unrealized
        -$241.56 instead of realized $780 + unreal -$241.56 =
        +$538.44)."""
        m = _ssm_with(log_rows=[
            {"date": TODAY, "portfolio": "paper",
             "ticker": "NFLX", "side": "LONG", "shares": 859,
             "entry_price": 87.59, "exit_price": 88.12,
             "entry_time": "10:16:42",
             "exit_time": "2026-05-12T14:29:00Z",
             "pnl": 450.80, "pnl_pct": 0.60, "reason": "target"},
            {"date": TODAY, "portfolio": "paper",
             "ticker": "MSFT", "side": "LONG", "shares": 25,
             "entry_price": 400.00, "exit_price": 413.17,
             "entry_time": "09:35:00",
             "exit_time": "2026-05-12T15:11:00Z",
             "pnl": 329.20, "pnl_pct": 3.29, "reason": "target"},
        ])
        assert _compute_realized_pnl_today(m, TODAY) == pytest.approx(780.0)

    def test_log_read_failure_falls_back_to_in_memory(self):
        m = _ssm_with(
            paper_trades=[{
                "action": "SELL", "ticker": "AAPL", "pnl": 100.0,
                "date": TODAY, "time": "10:00 ET",
            }],
            fail_log_read=True,
        )
        # Doesn't raise; returns the in-memory total alone
        assert _compute_realized_pnl_today(m, TODAY) == 100.0

    def test_log_row_for_other_day_ignored(self):
        m = _ssm_with(log_rows=[{
            "date": "2026-05-11", "ticker": "AAPL", "side": "LONG",
            "shares": 10, "entry_price": 150.0, "exit_price": 152.0,
            "exit_time": "2026-05-11T14:00:00Z",
            "pnl": 20.0,
        }])
        assert _compute_realized_pnl_today(m, TODAY) == 0.0

    def test_dedup_short_trade_history_wins(self):
        """Same dedup behavior for the short-side covers."""
        m = _ssm_with(
            short_history=[{
                "ticker": "NVDA", "pnl": 25.0, "date": TODAY,
                "time": "10:15 ET",
            }],
            log_rows=[{
                "date": TODAY, "portfolio": "paper",
                "ticker": "NVDA", "side": "SHORT", "shares": 5,
                "entry_price": 220.0, "exit_price": 215.0,
                "entry_time": "09:50:00",
                "exit_time": "2026-05-12T14:15:00Z",
                "pnl": 25.0,
            }],
        )
        assert _compute_realized_pnl_today(m, TODAY) == pytest.approx(25.0)

    def test_malformed_log_row_skipped(self):
        """Defensive: a malformed row in trade_log.jsonl doesn't
        break the sum."""
        m = _ssm_with(log_rows=[
            "not a dict",
            {"date": TODAY, "ticker": "AAPL", "pnl": "not a number",
             "exit_time": "2026-05-12T14:00:00Z"},
            {"date": TODAY, "ticker": "MSFT", "pnl": 100.0,
             "exit_time": "2026-05-12T15:00:00Z"},
        ])
        assert _compute_realized_pnl_today(m, TODAY) == 100.0
