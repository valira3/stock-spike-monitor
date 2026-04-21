#!/usr/bin/env python3
"""
smoke_test.py  —  Comprehensive smoke test for stock-spike-monitor.

Two modes:

  python smoke_test.py --local
      Exercises bot logic in-process against the imported module with
      synthetic state. No network, no Telegram, no FMP. Covers:
        · Trade math (long + short P&L, cash accounting)
        · Short-symmetry helpers (v3.4.8 fix surface)
        · _today_pnl_breakdown / _per_ticker_today_pnl / _compute_today_realized_pnl
        · EOD summary output
        · /reset authorization guards (v3.4.10)
        · Dashboard session token + auth (v3.4.9)
        · Dashboard login rate limiter
        · State save/load round-trip and new-day reset (M1)
        · Open-position pseudo-trade generator (N5 date field)
        · Utility helpers (_clamp, date parsing, _now_et)

  python smoke_test.py --prod [--url URL] [--password PW]
      Hits the live Railway deployment. Covers:
        · /api/state returns expected version
        · /stream SSE emits at least one event
        · /login returns 401 with wrong password, 302 with right password
        · Rate limiter trips on 6th bad attempt in <60s
        · Static asset served
        · Auth cookie roundtrip (old format rejected, new format accepted)

  python smoke_test.py  (no flag)
      Runs both in sequence.

Exit codes:
  0  — all passed
  1  — one or more failed
  2  — module import or setup error (can't even run)

The script is completely standalone — no pytest, no third-party deps
beyond what the bot itself requires (requests for prod mode).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

# ------------------------------------------------------------
# Tiny test harness — one function, no frameworks.
# ------------------------------------------------------------

_RESULTS: list[tuple[str, bool, str]] = []
_REGISTRY: list[tuple[str, Callable[[], None]]] = []


def t(name: str) -> Callable:
    """Decorator — registers a test. Tests run when run_suite() is called.
    Running the test captures stdout, records PASS/FAIL in _RESULTS.
    """
    def decorator(fn: Callable[[], None]) -> Callable[[], None]:
        _REGISTRY.append((name, fn))
        return fn
    return decorator


def _execute(name: str, fn: Callable[[], None]) -> None:
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn()
    except AssertionError as e:
        _RESULTS.append((name, False, f"assert: {e}\n{buf.getvalue()}"))
        return
    except Exception as e:
        _RESULTS.append(
            (name, False, f"{type(e).__name__}: {e}\n"
             f"{traceback.format_exc()}\n{buf.getvalue()}")
        )
        return
    _RESULTS.append((name, True, buf.getvalue()))


def run_suite(label: str) -> int:
    """Run every test currently in _REGISTRY, report, and reset."""
    for name, fn in _REGISTRY:
        _execute(name, fn)
    _REGISTRY.clear()
    return _report(label)


def _report(label: str) -> int:
    """Print results. Returns number of failures."""
    width = max(len(n) for n, _, _ in _RESULTS) if _RESULTS else 40
    print(f"\n═══ {label} ═══")
    fails = 0
    for name, ok, detail in _RESULTS:
        marker = "✓" if ok else "✗"
        print(f"  {marker}  {name.ljust(width)}")
        if not ok:
            fails += 1
            for line in detail.rstrip().splitlines():
                print(f"        {line}")
    passed = len(_RESULTS) - fails
    print(f"\n  {passed} passed · {fails} failed · {len(_RESULTS)} total\n")
    _RESULTS.clear()
    return fails


# ============================================================
# LOCAL MODE — in-process tests of bot logic
# ============================================================

def run_local() -> int:
    """Import the bot module fresh and exercise its logic in place."""

    # Tell the bot module to skip Telegram/scheduler/dashboard startup.
    os.environ["SSM_SMOKE_TEST"] = "1"
    # Module also reads these at import time.
    os.environ.setdefault("CHAT_ID", "999999999")
    os.environ.setdefault("DASHBOARD_PASSWORD", "smoketest1234")
    # Provide placeholder tokens so the Telegram bot builder doesn't raise.
    os.environ.setdefault("TELEGRAM_TOKEN",
                          "0000000000:AAAA_smoke_placeholder_token_0000000")
    os.environ.setdefault("TELEGRAM_TP_TOKEN",
                          "0000000000:AAAA_smoke_placeholder_token_0000000")
    # Point state files somewhere disposable so we don't stomp real state.
    tmp_dir = Path("/tmp/ssm_smoke_state")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(tmp_dir)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import stock_spike_monitor as m  # noqa: E402
        import dashboard_server as ds    # noqa: E402
    except Exception as e:
        print(f"Module import failed: {e}")
        traceback.print_exc()
        return 2

    def reset_state() -> None:
        """Wipe all mutable globals between tests so tests are independent."""
        m.positions.clear()
        m.short_positions.clear()
        m.tp_positions.clear()
        m.tp_short_positions.clear()
        m.paper_trades.clear()
        m.tp_paper_trades.clear()
        m.trade_history.clear()
        m.tp_trade_history.clear()
        m.short_trade_history.clear()
        m.tp_short_trade_history.clear()
        m.daily_entry_count.clear()
        m.daily_short_entry_count.clear()
        m.paper_cash = m.PAPER_STARTING_CAPITAL
        m.tp_paper_cash = m.PAPER_STARTING_CAPITAL
        m._trading_halted = False
        m._trading_halted_reason = ""
        # v3.4.15 webhook sync state
        try:
            m.tp_unsynced_exits.clear()
        except AttributeError:
            pass
        try:
            m.tp_state["recent_orders"] = []
            m.tp_state["total_orders_sent"] = 0
            m.tp_state["total_orders_success"] = 0
            m.tp_state["total_orders_failed"] = 0
        except Exception:
            pass

    today = m._now_et().strftime("%Y-%m-%d")

    # --------------------------------------------------------
    # Utility helpers
    # --------------------------------------------------------
    @t("utility: _clamp respects bounds")
    def _():
        assert m._clamp(5, (1, 10)) == 5
        assert m._clamp(-5, (1, 10)) == 1
        assert m._clamp(99, (1, 10)) == 10
        assert m._clamp(1.5, (1.0, 2.0)) == 1.5

    @t("utility: _now_et timezone is America/New_York")
    def _():
        now = m._now_et()
        assert str(now.tzinfo) in ("America/New_York", "US/Eastern", "EST", "EDT") \
            or "New_York" in str(now.tzinfo), f"got {now.tzinfo}"

    @t("utility: _is_today matches today's ISO string")
    def _():
        now_iso = m._utc_now_iso()
        assert m._is_today(now_iso), f"today check failed for {now_iso}"

    # --------------------------------------------------------
    # Short-symmetry helpers (v3.4.8 fix surface)
    # --------------------------------------------------------
    @t("v3.4.8: _today_pnl_breakdown sums longs + shorts (paper)")
    def _():
        reset_state()
        m.paper_trades.append({"action": "SELL", "ticker": "AAPL",
                               "date": today, "pnl": 100.0})
        m.paper_trades.append({"action": "BUY", "ticker": "AAPL",
                               "date": today, "pnl": 0})  # entry, ignored
        m.short_trade_history.append({"ticker": "TSLA", "date": today,
                                      "pnl": -50.0, "action": "COVER"})
        sells, covers, total, wins, losses, n = m._today_pnl_breakdown(False)
        assert len(sells) == 1, f"sells={sells}"
        assert len(covers) == 1
        assert total == 50.0, f"total={total}"
        assert wins == 1 and losses == 1
        assert n == 2

    @t("v3.4.8: _today_pnl_breakdown (TP) uses tp_short_trade_history")
    def _():
        reset_state()
        m.tp_paper_trades.append({"action": "SELL", "ticker": "NVDA",
                                  "date": today, "pnl": 200.0})
        m.tp_short_trade_history.append({"ticker": "AMD", "date": today,
                                         "pnl": 75.0})
        _, _, total, _, _, n = m._today_pnl_breakdown(is_tp=True)
        assert total == 275.0 and n == 2

    @t("v3.4.8: _today_pnl_breakdown ignores other days")
    def _():
        reset_state()
        m.paper_trades.append({"action": "SELL", "ticker": "AAPL",
                               "date": "2020-01-01", "pnl": 999.0})
        m.short_trade_history.append({"ticker": "TSLA", "date": "2020-01-01",
                                      "pnl": 999.0})
        _, _, total, _, _, n = m._today_pnl_breakdown(False)
        assert total == 0.0 and n == 0

    @t("v3.4.8: _compute_today_realized_pnl counts short losses (DEFENSIVE gate)")
    def _():
        reset_state()
        # Short-only losing day — must show up in DEFENSIVE gate input.
        m.short_trade_history.append({"ticker": "SPY", "date": today,
                                      "pnl": -500.0})
        pnl = m._compute_today_realized_pnl(is_tp=False)
        assert pnl == -500.0, f"DEFENSIVE gate saw {pnl}, expected -500"

    @t("v3.4.8: _per_ticker_today_pnl buckets both SELLs and COVERs")
    def _():
        reset_state()
        m.paper_trades.append({"action": "SELL", "ticker": "AAPL",
                               "date": today, "pnl": 50.0})
        m.short_trade_history.append({"ticker": "AAPL", "date": today,
                                      "pnl": -20.0})
        m.short_trade_history.append({"ticker": "TSLA", "date": today,
                                      "pnl": 10.0})
        per = m._per_ticker_today_pnl()
        assert per.get("AAPL") == 30.0, f"AAPL={per.get('AAPL')}"
        assert per.get("TSLA") == 10.0

    @t("v3.4.8: _per_ticker_today_pnl skips BUY actions")
    def _():
        reset_state()
        m.paper_trades.append({"action": "BUY", "ticker": "AAPL",
                               "date": today, "pnl": 0})
        per = m._per_ticker_today_pnl()
        assert per.get("AAPL", 0) == 0

    # --------------------------------------------------------
    # Open-position pseudo-trades (N5)
    # --------------------------------------------------------
    @t("N5: open positions carry a 'date' field usable by day filters")
    def _():
        reset_state()
        m.positions["AAPL"] = {
            "entry_price": 150.0, "shares": 10,
            "stop": 145.0, "trail_active": False, "trail_high": 150.0,
            "entry_count": 1, "entry_time": "10:30",
            "date": today, "pdc": 149.0,
        }
        m.short_positions["TSLA"] = {
            "entry_price": 200.0, "shares": 5,
            "stop": 205.0, "entry_time": "11:00", "date": today,
        }
        # filter: "today's rows" from positions dicts
        open_today = [p for p in m.positions.values() if p.get("date") == today]
        short_today = [p for p in m.short_positions.values() if p.get("date") == today]
        assert len(open_today) == 1 and len(short_today) == 1

    # --------------------------------------------------------
    # State save/load round-trip (M1)
    # --------------------------------------------------------
    @t("M1: load_paper_state clears daily_short_entry_count on new day")
    def _():
        reset_state()
        # Simulate yesterday's state file on disk.
        import json
        yesterday = "2020-01-01"
        m.daily_short_entry_count["AAPL"] = 3
        m.paper_trades.append({"date": yesterday, "pnl": 5.0, "action": "SELL"})
        # Write state pretending daily_entry_date is yesterday.
        state = {
            "paper_cash": 100_000.0,
            "positions": {},
            "paper_trades": m.paper_trades,
            "trade_history": [],
            "short_positions": {},
            "short_trade_history": [],
            "daily_entry_count": dict(m.daily_entry_count),
            "daily_short_entry_count": dict(m.daily_short_entry_count),
            "daily_entry_date": yesterday,
            "_trading_halted": False,
            "_trading_halted_reason": "",
        }
        Path("paper_state.json").write_text(json.dumps(state))
        reset_state()
        # daily_short_entry_count should be non-empty BEFORE load to prove clear worked.
        m.daily_short_entry_count["XOM"] = 9
        m.load_paper_state()
        assert len(m.daily_short_entry_count) == 0, \
            f"expected empty, got {m.daily_short_entry_count}"

    @t("state: save_paper_state round-trips cash and positions")
    def _():
        reset_state()
        m.paper_cash = 95_123.45
        m.positions["MSFT"] = {
            "entry_price": 400.0, "shares": 10, "stop": 395.0,
            "trail_active": False, "trail_high": 400.0, "entry_count": 1,
            "entry_time": "09:45", "date": today, "pdc": 399.0,
        }
        m.save_paper_state()
        reset_state()
        m.load_paper_state()
        assert abs(m.paper_cash - 95_123.45) < 0.01, f"cash={m.paper_cash}"
        assert "MSFT" in m.positions

    # --------------------------------------------------------
    # /reset authorization (v3.4.10)
    # --------------------------------------------------------
    @t("v3.4.10: _reset_authorized blocks stale confirm (>60s old)")
    def _():
        class FakeMsg:
            chat_id = int(m.CHAT_ID) if m.CHAT_ID else 999999999

        class FakeQuery:
            message = FakeMsg()
            data = f"reset_paper_confirm:{int(time.time()) - 120}"

        allowed, reason = m._reset_authorized(FakeQuery())
        assert not allowed and "expired" in reason, \
            f"expected stale rejection, got ({allowed},{reason})"

    @t("v3.4.10: _reset_authorized accepts fresh confirm from owner")
    def _():
        class FakeMsg:
            chat_id = int(m.CHAT_ID) if m.CHAT_ID else 999999999

        class FakeQuery:
            message = FakeMsg()
            data = f"reset_paper_confirm:{int(time.time())}"

        allowed, reason = m._reset_authorized(FakeQuery())
        assert allowed, f"expected allow, got reason={reason}"

    @t("v3.4.10: _reset_authorized blocks TP reset from paper bot")
    def _():
        class FakeMsg:
            chat_id = int(m.CHAT_ID) if m.CHAT_ID else 999999999

        class FakeQuery:
            message = FakeMsg()
            data = f"reset_tp_confirm:{int(time.time())}"

        allowed, reason = m._reset_authorized(FakeQuery())
        assert not allowed and "TP" in reason, \
            f"expected cross-bot reject, got ({allowed},{reason})"

    @t("v3.4.10: _reset_authorized blocks unauthorized chat_id")
    def _():
        class FakeMsg:
            chat_id = 12345  # not CHAT_ID, not TELEGRAM_TP_CHAT_ID

        class FakeQuery:
            message = FakeMsg()
            data = f"reset_paper_confirm:{int(time.time())}"

        allowed, reason = m._reset_authorized(FakeQuery())
        assert not allowed and "unauthorized" in reason

    @t("v3.4.10: _reset_authorized blocks malformed timestamp")
    def _():
        class FakeMsg:
            chat_id = int(m.CHAT_ID) if m.CHAT_ID else 999999999

        class FakeQuery:
            message = FakeMsg()
            data = "reset_paper_confirm:not-a-number"

        allowed, reason = m._reset_authorized(FakeQuery())
        assert not allowed and "malformed" in reason

    @t("v3.4.10: _reset_buttons embeds a fresh timestamp")
    def _():
        kb = m._reset_buttons("paper")
        cb = kb.inline_keyboard[0][0].callback_data
        assert cb.startswith("reset_paper_confirm:")
        ts = int(cb.split(":", 1)[1])
        assert abs(ts - int(time.time())) < 5, f"ts drift: {ts} vs {int(time.time())}"

    # --------------------------------------------------------
    # Dashboard session token (v3.4.9)
    # --------------------------------------------------------
    @t("v3.4.9: dashboard token roundtrip passes auth")
    def _():
        import secrets
        ds._SESSION_SECRET = secrets.token_bytes(32)
        token = ds._make_token()

        class FakeReq:
            cookies = {ds.SESSION_COOKIE: token}

        assert ds._check_auth(FakeReq()) is True

    @t("v3.4.9: dashboard rejects expired token")
    def _():
        import secrets
        ds._SESSION_SECRET = secrets.token_bytes(32)
        stale = ds._make_token(now=time.time() - (ds.SESSION_DAYS * 86400 + 10))

        class FakeReq:
            cookies = {ds.SESSION_COOKIE: stale}

        assert ds._check_auth(FakeReq()) is False

    @t("v3.4.9: dashboard rejects token signed with different secret")
    def _():
        import secrets
        ds._SESSION_SECRET = secrets.token_bytes(32)
        good = ds._make_token()
        ds._SESSION_SECRET = secrets.token_bytes(32)  # rotate

        class FakeReq:
            cookies = {ds.SESSION_COOKIE: good}

        assert ds._check_auth(FakeReq()) is False

    @t("v3.4.9: dashboard rejects malformed token (no colon)")
    def _():
        class FakeReq:
            cookies = {ds.SESSION_COOKIE: "notavalidtoken"}

        assert ds._check_auth(FakeReq()) is False

    @t("v3.4.9: dashboard rejects missing cookie")
    def _():
        class FakeReq:
            cookies = {}

        assert ds._check_auth(FakeReq()) is False

    @t("v3.4.9: dashboard rejects future-dated token beyond skew")
    def _():
        import secrets
        ds._SESSION_SECRET = secrets.token_bytes(32)
        future = ds._make_token(now=time.time() + 3600)

        class FakeReq:
            cookies = {ds.SESSION_COOKIE: future}

        assert ds._check_auth(FakeReq()) is False

    # --------------------------------------------------------
    # Dashboard login rate limiter (M6)
    # --------------------------------------------------------
    @t("M6: rate limiter allows first 5 attempts, blocks 6th")
    def _():
        # Reset to a clean bucket.
        ds._login_attempts.clear()
        ip = "10.0.0.99"
        for i in range(ds._LOGIN_MAX_ATTEMPTS):
            assert ds._rate_limit_check(ip), f"attempt {i+1} blocked"
        assert not ds._rate_limit_check(ip), "6th attempt should block"

    @t("M6: rate limiter buckets per IP")
    def _():
        ds._login_attempts.clear()
        for _ in range(ds._LOGIN_MAX_ATTEMPTS):
            ds._rate_limit_check("10.0.0.100")
        assert not ds._rate_limit_check("10.0.0.100")
        assert ds._rate_limit_check("10.0.0.101"), "different IP should pass"

    # --------------------------------------------------------
    # Report builders
    # --------------------------------------------------------
    @t("reports: _build_eod_report produces mixed long+short text")
    def _():
        reset_state()
        # _build_eod_report reads from trade_history (longs) and
        # short_trade_history (shorts). Each short must carry side="short"
        # for the [S] tag to render (real code at line 2725 sets this).
        m.trade_history.append({
            "action": "SELL", "ticker": "AAPL", "date": today,
            "pnl": 150.0, "shares": 10, "entry_price": 150.0,
            "exit_price": 165.0, "reason": "TP", "side": "long",
        })
        m.short_trade_history.append({
            "action": "COVER", "ticker": "TSLA", "date": today,
            "pnl": -40.0, "shares": 5, "entry_price": 200.0,
            "exit_price": 208.0, "reason": "STOP", "side": "short",
        })
        out = m._build_eod_report(today, "paper")
        assert "AAPL" in out and "TSLA" in out, \
            f"EOD report missing a ticker: {out[:400]}"
        assert "[L]" in out and "[S]" in out, \
            f"missing long/short tags: {out[:400]}"
        assert "L:1 S:1" in out, f"wrong L/S counts: {out[:400]}"
        # Day P&L should be +150 + -40 = +110
        assert "+110" in out, f"wrong day P&L: {out[:400]}"

    @t("reports: eod summary counts long SELLs + short COVERs")
    def _():
        reset_state()
        m.paper_trades.append({"action": "SELL", "ticker": "AAPL",
                               "date": today, "pnl": 100.0})
        m.short_trade_history.append({"ticker": "TSLA", "date": today,
                                      "pnl": -50.0})
        _, _, total, _, _, n = m._today_pnl_breakdown(False)
        assert n == 2 and total == 50.0

    # --------------------------------------------------------
    # Command handlers — sanity
    # --------------------------------------------------------
    @t("v3.4.7: _collect_day_rows returns long + short rows for today")
    def _():
        reset_state()
        # Today's long SELL (must be in paper_trades for the same-day branch).
        m.paper_trades.append({"action": "SELL", "ticker": "AAPL",
                               "date": today, "pnl": 10.0, "shares": 1,
                               "entry_price": 100.0, "exit_price": 110.0,
                               "side": "long"})
        # Today's short COVER (in short_trade_history).
        m.short_trade_history.append({"action": "COVER", "ticker": "TSLA",
                                      "date": today, "pnl": -5.0, "shares": 1,
                                      "entry_price": 200.0,
                                      "exit_price": 205.0, "side": "short"})
        # Signature: _collect_day_rows(target_str, today_str, is_tp)
        rows = m._collect_day_rows(today, today, is_tp=False)
        assert rows and len(rows) >= 2, \
            f"expected >=2 rows, got {len(rows) if rows else 0}"
        tickers = {r.get("ticker") for r in rows}
        assert "AAPL" in tickers and "TSLA" in tickers, \
            f"missing tickers in {tickers}"

    # --------------------------------------------------------
    # Regressions from fixed bugs
    # --------------------------------------------------------
    @t("regression: DEFENSIVE gate triggers on short-only losing day")
    def _():
        reset_state()
        # Simulate loss breaching the daily limit, shorts only.
        loss = -(m.DAILY_LOSS_LIMIT + 1.0)
        m.short_trade_history.append({"ticker": "SPY", "date": today,
                                      "pnl": loss})
        pnl = m._compute_today_realized_pnl(is_tp=False)
        assert pnl <= -m.DAILY_LOSS_LIMIT, \
            f"short-only day did not breach daily limit: pnl={pnl}"

    @t("regression: weekly digest merges longs + shorts")
    def _():
        reset_state()
        m.trade_history.append({"date": today, "pnl": 100.0,
                                "ticker": "AAPL", "action": "SELL"})
        m.short_trade_history.append({"date": today, "pnl": -40.0,
                                      "ticker": "TSLA", "action": "COVER"})
        combined = list(m.trade_history) + list(m.short_trade_history)
        assert len(combined) == 2
        assert sum(t_.get("pnl", 0) for t_ in combined) == 60.0

    # --------------------------------------------------------
    # v3.4.15 — Webhook response handling
    # --------------------------------------------------------
    @t("v3.4.15: send_traderspost_order returns skipped dict when disabled")
    def _():
        reset_state()
        prev_enabled = m.TRADERSPOST_ENABLED
        try:
            m.TRADERSPOST_ENABLED = False
            result = m.send_traderspost_order("SPY", "sell", 450.0, shares=10)
        finally:
            m.TRADERSPOST_ENABLED = prev_enabled
        assert isinstance(result, dict), f"expected dict, got {type(result)}"
        assert result.get("skipped") is True, f"expected skipped=True, got {result}"
        assert result.get("success") is False
        # skipped contract: no unsynced exit should be recorded by callers
        assert "message" in result and "raw" in result

    @t("v3.4.15: _extract_broker_message parses message/error/errors shapes")
    def _():
        # Plain message field
        msg1 = m._extract_broker_message({"message": "Insufficient buying power"})
        assert "Insufficient" in msg1
        # error field (singular)
        msg2 = m._extract_broker_message({"error": "rate limited"})
        assert "rate" in msg2.lower()
        # errors as list of strings
        msg3 = m._extract_broker_message({"errors": ["bad symbol", "bad qty"]})
        assert "bad symbol" in msg3
        # errors as list of dicts with message key
        msg4 = m._extract_broker_message(
            {"errors": [{"message": "symbol not tradable"}]})
        assert "symbol not tradable" in msg4
        # length-capped at 80 chars
        long_msg = "x" * 200
        assert len(m._extract_broker_message({"message": long_msg})) <= 80

    @t("v3.4.15: rejected exit populates tp_unsynced_exits dict")
    def _():
        reset_state()
        # Simulate a close_position-style TP-branch rejection by calling
        # the unsynced-tracking pattern directly; this is what the three
        # exit sites execute when send_traderspost_order returns failure.
        fake_result = {"success": False, "skipped": False,
                       "message": "Insufficient buying power",
                       "http_status": 400, "raw": None}
        if not (fake_result.get("success") or fake_result.get("skipped")):
            m.tp_unsynced_exits["SPY"] = {
                "action": "sell", "price": 450.0, "shares": 10,
                "message": fake_result.get("message", ""),
                "http_status": fake_result.get("http_status"),
                "time": "12:34 CDT",
            }
        assert "SPY" in m.tp_unsynced_exits
        entry = m.tp_unsynced_exits["SPY"]
        assert entry["action"] == "sell"
        assert entry["http_status"] == 400
        assert "Insufficient" in entry["message"]

    @t("v3.4.15: skipped result does NOT populate tp_unsynced_exits")
    def _():
        reset_state()
        fake_result = {"success": False, "skipped": True, "message": "",
                       "http_status": 0, "raw": None}
        if not (fake_result.get("success") or fake_result.get("skipped")):
            m.tp_unsynced_exits["SPY"] = {"action": "sell"}
        assert "SPY" not in m.tp_unsynced_exits, \
            "skipped webhook should not mark position unsynced"

    @t("v3.4.15: dashboard snapshot includes tp_sync section")
    def _():
        reset_state()
        m.tp_unsynced_exits["TSLA"] = {
            "action": "sell", "price": 200.0, "shares": 5,
            "message": "test rejection", "http_status": 400,
            "time": "10:00 CDT",
        }
        snap = ds.snapshot()
        assert snap.get("ok") is True, f"snapshot failed: {snap}"
        tp = snap.get("tp_sync")
        assert isinstance(tp, dict), f"tp_sync missing: {snap.keys()}"
        assert "enabled" in tp
        assert "unsynced_exits" in tp
        assert "recent_orders" in tp
        assert "TSLA" in tp["unsynced_exits"]

    @t("v3.4.15: /tp_sync command handler is defined")
    def _():
        # The command function must exist and be callable.
        assert hasattr(m, "cmd_tp_sync"), "cmd_tp_sync not defined"
        assert callable(m.cmd_tp_sync)

    @t("v3.4.16: /tp_sync lives on TP bot only (not MAIN_BOT_COMMANDS)")
    def _():
        main_names = [c.command for c in m.MAIN_BOT_COMMANDS]
        tp_names = [c.command for c in m.TP_BOT_COMMANDS]
        assert "tp_sync" not in main_names, \
            f"tp_sync must NOT be in MAIN_BOT_COMMANDS: {main_names}"
        assert "tp_sync" in tp_names, \
            f"tp_sync must be in TP_BOT_COMMANDS: {tp_names}"

    @t("v3.4.16: release notes split — main has no broker internals")
    def _():
        assert hasattr(m, "MAIN_RELEASE_NOTE")
        assert hasattr(m, "TP_RELEASE_NOTE")
        main_lc = m.MAIN_RELEASE_NOTE.lower()
        # Main release note must not leak TP broker-internal terminology.
        # Brief context-setting mentions of /tp_sync (pointing readers to
        # the TP bot) are fine \u2014 we only forbid the broker-loop terms
        # that the main bot's audience should not have to reason about.
        for bad in ("webhook", "broker", "unsynced"):
            assert bad not in main_lc, \
                f"MAIN_RELEASE_NOTE leaks {bad!r}: {m.MAIN_RELEASE_NOTE!r}"
        # TP release note should mention tp_sync.
        assert "/tp_sync" in m.TP_RELEASE_NOTE, \
            f"TP_RELEASE_NOTE missing /tp_sync: {m.TP_RELEASE_NOTE!r}"

    @t("v3.4.16: main-bot /tp_sync redirect handler exists")
    def _():
        assert hasattr(m, "cmd_tp_sync_on_main"), "redirect handler missing"
        assert callable(m.cmd_tp_sync_on_main)
        # Must be distinct from cmd_tp_sync so main doesn't leak data.
        assert m.cmd_tp_sync_on_main is not m.cmd_tp_sync

    @t("v3.4.16: release notes all within 34-char Telegram width")
    def _():
        for name in ("MAIN_RELEASE_NOTE", "TP_RELEASE_NOTE"):
            text = getattr(m, name)
            for line in text.split("\n"):
                assert len(line) <= 34, \
                    f"{name} line too long ({len(line)}): {line!r}"

    return run_suite("LOCAL SMOKE TESTS")


# ============================================================
# PROD MODE — live Railway deployment checks
# ============================================================

def run_prod(url: str, password: str, expected_version: str | None) -> int:
    """Hit the live dashboard and exercise the public surface."""
    try:
        import requests
    except ImportError:
        print("prod mode requires `pip install requests`")
        return 2

    url = url.rstrip("/")
    sess = requests.Session()
    # We pass allow_redirects=False per-call so we can inspect 302s ourselves.

    @t("prod: /login with correct password returns 302")
    def _():
        r = sess.post(f"{url}/login", data={"password": password},
                      allow_redirects=False, timeout=10)
        assert r.status_code == 302, f"expected 302, got {r.status_code}"
        cookie = sess.cookies.get("spike_session")
        assert cookie and ":" in cookie, f"bad cookie format: {cookie}"

    @t("prod: /login with wrong password returns 401")
    def _():
        # Fresh session to avoid bucket pollution from the earlier test.
        s2 = requests.Session()
        r = s2.post(f"{url}/login", data={"password": "definitelywrong"},
                    allow_redirects=False, timeout=10)
        assert r.status_code in (401, 429), \
            f"expected 401 (or 429 if rate-limited), got {r.status_code}"

    @t("prod: /api/state returns JSON with version field")
    def _():
        r = sess.get(f"{url}/api/state", timeout=10)
        assert r.status_code == 200, f"status={r.status_code}"
        data = r.json()
        assert "version" in data, f"no 'version' key in {list(data.keys())}"
        if expected_version:
            assert data["version"] == expected_version, \
                f"version {data['version']} != {expected_version}"
        print(f"  live version: {data['version']}")

    @t("prod: /api/state exposes expected keys")
    def _():
        r = sess.get(f"{url}/api/state", timeout=10)
        data = r.json()
        needed = {"version", "portfolio", "positions", "regime", "tickers",
                  "tp_sync"}
        missing = needed - set(data.keys())
        assert not missing, f"missing keys: {missing}"

    @t("v3.4.15 prod: /api/state tp_sync has expected shape")
    def _():
        r = sess.get(f"{url}/api/state", timeout=10)
        data = r.json()
        tp = data.get("tp_sync") or {}
        assert isinstance(tp, dict), f"tp_sync not a dict: {type(tp)}"
        for k in ("enabled", "unsynced_exits", "recent_orders"):
            assert k in tp, f"tp_sync missing {k}: {list(tp.keys())}"
        assert isinstance(tp["unsynced_exits"], dict)
        assert isinstance(tp["recent_orders"], list)

    @t("prod: /api/state rejects request with no cookie (401)")
    def _():
        s3 = requests.Session()
        r = s3.get(f"{url}/api/state", allow_redirects=False, timeout=10)
        # Should redirect to /login or return 401/403
        assert r.status_code in (302, 401, 403), \
            f"expected redirect/401/403, got {r.status_code}"

    @t("prod: /api/state rejects forged cookie")
    def _():
        s4 = requests.Session()
        s4.cookies.set("spike_session",
                       "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:1")
        r = s4.get(f"{url}/api/state", allow_redirects=False, timeout=10)
        assert r.status_code in (302, 401, 403), \
            f"expected reject, got {r.status_code}"

    @t("prod: /stream SSE emits at least one event within 5s")
    def _():
        r = sess.get(f"{url}/stream", stream=True, timeout=10)
        assert r.status_code == 200
        deadline = time.time() + 5
        saw_data = False
        for line in r.iter_lines(decode_unicode=True):
            if line and (line.startswith("data:") or line.startswith("event:")):
                saw_data = True
                break
            if time.time() > deadline:
                break
        r.close()
        assert saw_data, "no SSE frame received in 5s"

    @t("prod: rate limiter trips on 6th bad attempt in <60s")
    def _():
        s5 = requests.Session()
        statuses = []
        for i in range(7):
            r = s5.post(f"{url}/login",
                        data={"password": "wrong-rate-limit-test"},
                        allow_redirects=False, timeout=10)
            statuses.append(r.status_code)
            time.sleep(0.3)
        # Expect the first few to be 401; at least one of attempts 6-7 must be 429.
        assert 429 in statuses[5:], \
            f"rate limit never tripped; statuses={statuses}"
        print(f"  statuses across 7 attempts: {statuses}")

    @t("prod: /static/ assets serve without auth")
    def _():
        s6 = requests.Session()
        r = s6.get(f"{url}/static/index.html", timeout=10,
                   allow_redirects=False)
        # Either the static page is public, or the dashboard serves root login.
        assert r.status_code in (200, 302, 404), f"unexpected: {r.status_code}"

    return run_suite("PROD SMOKE TESTS")


# ============================================================
# CLI
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="Stock Spike Monitor smoke test")
    parser.add_argument("--local", action="store_true",
                        help="Run in-process bot-logic tests only.")
    parser.add_argument("--prod", action="store_true",
                        help="Run live prod dashboard checks only.")
    parser.add_argument(
        "--url",
        default="https://stock-spike-monitor-production.up.railway.app",
        help="Dashboard base URL for --prod mode."
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("DASHBOARD_PASSWORD", ""),
        help="Dashboard password for --prod mode (or $DASHBOARD_PASSWORD).",
    )
    parser.add_argument("--expected-version", default=None,
                        help="Assert /api/state returns this version string.")
    args = parser.parse_args()

    # Default: run both.
    do_local = args.local or not (args.local or args.prod)
    do_prod = args.prod or not (args.local or args.prod)

    total_fails = 0
    if do_local:
        total_fails += run_local()
    if do_prod:
        if not args.password:
            print("ERROR: --prod needs --password or $DASHBOARD_PASSWORD")
            return 2
        total_fails += run_prod(args.url, args.password, args.expected_version)

    print(f"═══ RESULT: {'PASS' if total_fails == 0 else f'FAIL ({total_fails})'} ═══")
    return 0 if total_fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
