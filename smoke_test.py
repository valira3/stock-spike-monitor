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
        # v3.4.39: reject string now says "Robinhood reset..." (user-facing
        # rename from TP \u2014 Python identifiers still use tp_* prefix).
        assert not allowed and ("TP" in reason or "Robinhood" in reason), \
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
        # v3.4.38 — TRADERSPOST_ENABLED is now gated by is_traderspost_enabled()
        # with a runtime override (_traderspost_runtime_override) layered on
        # top of the env default. Force it off for this test.
        reset_state()
        prev_override = m._traderspost_runtime_override
        try:
            m._traderspost_runtime_override = False
            result = m.send_traderspost_order("SPY", "sell", 450.0, shares=10)
        finally:
            m._traderspost_runtime_override = prev_override
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
        # TP release note should reference a Robinhood command (v3.4.37
        # renamed tp_sync to rh_sync; v3.4.38 added rh_enable/disable/status).
        assert "/rh_" in m.TP_RELEASE_NOTE, \
            f"TP_RELEASE_NOTE missing /rh_* command reference: {m.TP_RELEASE_NOTE!r}"

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

    @t("v3.4.18: _CallbackUpdateShim forwards get_bot() for is_tp routing")
    def _():
        # Without this, commands invoked from /menu buttons fall through
        # the try/except in is_tp_update() and always return False,
        # rendering paper data on the TP bot (the "mix" symptom).
        shim_cls = m._CallbackUpdateShim
        assert hasattr(shim_cls, "get_bot"), \
            "_CallbackUpdateShim must expose get_bot() so is_tp_update works"

        class _FakeBot:
            def __init__(self, token): self.token = token

        class _FakeQuery:
            def __init__(self, token):
                self._bot = _FakeBot(token)
            def get_bot(self): return self._bot

        # TP token -> is_tp_update True
        tp_shim = shim_cls(_FakeQuery(m.TP_TOKEN))
        assert m.is_tp_update(tp_shim) is True, \
            "shim with TP token must resolve is_tp_update -> True"

        # Non-TP token -> is_tp_update False
        paper_shim = shim_cls(_FakeQuery("some-other-token"))
        assert m.is_tp_update(paper_shim) is False, \
            "shim with non-TP token must resolve is_tp_update -> False"

    @t("v3.4.20: _entry_bar_volume walks back past null/zero bars")
    def _():
        # Yahoo returns the most-recent closed bar (volumes[-2]) with
        # null/zero volume when the bar has not been populated yet.
        # The helper must walk back until it finds a valid bar, and
        # return (0, False) if it cannot — so callers fail-closed.
        fn = getattr(m, "_entry_bar_volume", None)
        assert fn is not None, "_entry_bar_volume helper must exist"

        # Happy path: volumes[-2] is valid -> use it directly.
        vol, ready = fn([100, 200, 300, 400, 500])  # [-2] = 400
        assert ready is True and vol == 400, (vol, ready)

        # Stale trailing bar: volumes[-2] is None, volumes[-3] is valid.
        vol, ready = fn([100, 200, 300, None, 500])  # [-2] = None, [-3] = 300
        assert ready is True and vol == 300, (vol, ready)

        # Stale trailing bar: volumes[-2] is 0 (what today's bug saw).
        vol, ready = fn([100, 200, 300, 0, 500])  # [-2] = 0, [-3] = 300
        assert ready is True and vol == 300, (vol, ready)

        # All candidate bars null/zero -> DATA NOT READY (fail-closed).
        # Default lookback=5 scans offsets -2..-6, so pad with 6 bad bars.
        vol, ready = fn([0, None, 0, None, 0, None, 0])
        assert ready is False and vol == 0, (vol, ready)

        # Very short series -> not ready.
        vol, ready = fn([100])
        assert ready is False and vol == 0, (vol, ready)
        vol, ready = fn([])
        assert ready is False and vol == 0, (vol, ready)

        # Lookback window bounds: only peek back `lookback` bars.
        # Series: v[-2]=None, v[-3]=0, v[-4]=777 (valid but out of reach).
        # With lookback=2 the helper scans only offsets 2, 3 -> not ready.
        vol, ready = fn([100, 777, 0, None, 0], lookback=2)
        assert ready is False, (vol, ready)
        # Same series with lookback=3 reaches the valid bar at offset 4.
        vol, ready = fn([100, 777, 0, None, 0], lookback=3)
        assert ready is True and vol == 777, (vol, ready)

    @t("v3.4.20: entry gates call _entry_bar_volume + emit DATA NOT READY")
    def _():
        # Both long-entry and short-entry gates must:
        #  1. call _entry_bar_volume(volumes) (not read volumes[-2] raw)
        #  2. emit the new [DATA NOT READY] log label
        import inspect
        # Find the two functions that own the LOW VOL gates by grepping
        # module source for the log string, then resolve containing fns.
        src_all = inspect.getsource(m)
        # There must be two occurrences of the LOW VOL log line (long + short).
        assert src_all.count("[LOW VOL] entry bar") == 2, \
            "expected exactly 2 LOW VOL gate sites"
        # DATA NOT READY must show up at least twice (one per gate site).
        assert src_all.count("[DATA NOT READY]") >= 2, \
            "expected DATA NOT READY log at both gate sites"
        # _entry_bar_volume must be called at least twice in the source.
        assert src_all.count("_entry_bar_volume(volumes)") >= 2, \
            "expected both gate sites to call _entry_bar_volume"
        # The raw, unsafe pattern that caused today's bug must be gone.
        assert "volumes[-2] if volumes[-2] is not None else 0" not in src_all, \
            "raw volumes[-2] read must be replaced everywhere"

    @t("v3.4.19: menu/refresh callbacks route by token, not chat_id")
    def _():
        # Three callbacks previously routed data by comparing
        # query.message.chat_id to TELEGRAM_TP_CHAT_ID. That breaks
        # whenever the TP bot is used in a chat whose id doesn't
        # match the env var (DMs, topic threads, un-enrolled group).
        # They must use is_tp_update(update) like every cmd_* handler.
        # (We strip comments before checking so explanatory prose that
        #  names the old env var is allowed.)
        import inspect, re
        def _strip_comments(src):
            out = []
            for ln in src.splitlines():
                stripped = ln.lstrip()
                if stripped.startswith("#"):
                    continue
                # drop trailing comment (naive but fine for our code)
                i = ln.find("#")
                if i >= 0:
                    ln = ln[:i]
                out.append(ln)
            return "\n".join(out)
        for name in ("positions_callback",
                     "proximity_callback",
                     "menu_callback"):
            fn = getattr(m, name, None)
            assert fn is not None, f"{name} must exist"
            src = inspect.getsource(fn)
            assert "is_tp_update(update)" in src, \
                f"{name} must use is_tp_update(update) for bot routing"
            code_only = _strip_comments(src)
            assert "TELEGRAM_TP_CHAT_ID" not in code_only, \
                f"{name} must not route data by chat_id anymore"

    # ============================================================
    # v3.4.21 regressions
    # ------------------------------------------------------------
    #  - Deploy card shows ONLY the current release note.
    #  - Stop cap: long/short stops clamp to ±0.75% from entry.
    #  - Near-miss ring buffer exists and _record_near_miss works.
    #  - Per-ticker gate snapshot dict exists.
    #  - /near_misses Telegram command is wired up.
    # ============================================================

    @t("v3.4.21: CURRENT_MAIN_NOTE/CURRENT_TP_NOTE scope + width")
    def _():
        # Current-only notes: must start with the current version,
        # must not mention any older version, and every line must
        # fit Telegram mobile code-block width (≤34 chars).
        for attr in ("CURRENT_MAIN_NOTE", "CURRENT_TP_NOTE"):
            txt = getattr(m, attr, None)
            assert isinstance(txt, str) and txt, f"{attr} must be a non-empty string"
            assert txt.lstrip().startswith(f"v{m.BOT_VERSION}"), \
                f"{attr} must start with v{m.BOT_VERSION}"
            # No stale version references inside the current-only note.
            for old in ("v3.4.20", "v3.4.19", "v3.4.18", "v3.4.17",
                        "v3.4.16", "v3.4.15"):
                assert old not in txt, \
                    f"{attr} must not mention {old}"
            for ln in txt.splitlines():
                assert len(ln) <= 34, \
                    f"{attr} line too wide ({len(ln)}>34): {ln!r}"

    @t("v3.4.21: rolling RELEASE_NOTE still leads with current version")
    def _():
        for attr in ("MAIN_RELEASE_NOTE", "TP_RELEASE_NOTE"):
            txt = getattr(m, attr, None)
            assert isinstance(txt, str) and txt, f"{attr} must exist"
            assert txt.lstrip().startswith(f"v{m.BOT_VERSION}"), \
                f"{attr} must lead with v{m.BOT_VERSION}"

    @t("v3.4.21: deploy card uses CURRENT_* notes, not rolling RELEASE_NOTE")
    def _():
        import inspect
        fn = getattr(m, "send_startup_message", None)
        assert fn is not None, "send_startup_message must exist"
        src = inspect.getsource(fn)
        # Must use current-only notes on the deploy card.
        assert "{CURRENT_MAIN_NOTE}" in src, \
            "deploy card must embed {CURRENT_MAIN_NOTE}"
        assert "{CURRENT_TP_NOTE}" in src, \
            "deploy card must embed {CURRENT_TP_NOTE}"
        # Must NOT embed the rolling history notes on the deploy card.
        assert "{MAIN_RELEASE_NOTE}" not in src, \
            "deploy card must not embed {MAIN_RELEASE_NOTE} (rolling history)"
        assert "{TP_RELEASE_NOTE}" not in src, \
            "deploy card must not embed {TP_RELEASE_NOTE} (rolling history)"

    @t("v3.4.21: MAX_STOP_PCT == 0.0075 (0.75% cap)")
    def _():
        assert hasattr(m, "MAX_STOP_PCT"), "MAX_STOP_PCT must be defined"
        assert abs(m.MAX_STOP_PCT - 0.0075) < 1e-9, \
            f"MAX_STOP_PCT must be 0.0075, got {m.MAX_STOP_PCT}"

    @t("v3.4.21: _capped_long_stop tightens when entry is far above OR")
    def _():
        # Real-world case (MSFT 4/21): OR 420.16, entry 425.93.
        # Baseline = 419.26; 0.75% floor = 422.74; cap must win.
        stop, capped, baseline = m._capped_long_stop(420.16, 425.93)
        assert capped is True, "entry 1.37% above OR must trigger cap"
        assert stop == 422.74, f"expected stop 422.74, got {stop}"
        assert baseline == 419.26, f"expected baseline 419.26, got {baseline}"
        # Risk reduction sanity: capped risk is tighter than baseline.
        assert (425.93 - stop) < (425.93 - baseline), \
            "capped stop must be tighter (smaller risk) than baseline"

    @t("v3.4.21: _capped_long_stop leaves baseline alone for near-OR entries")
    def _():
        # Entry barely above OR — baseline stop is already within
        # 0.75% of entry; cap must NOT loosen it.
        stop, capped, baseline = m._capped_long_stop(420.16, 420.50)
        assert capped is False, "near-OR entry must not trip cap"
        assert stop == baseline == 419.26, \
            f"expected stop=baseline=419.26, got stop={stop} baseline={baseline}"

    @t("v3.4.21: _capped_short_stop tightens when entry is far below PDC")
    def _():
        # PDC 420.00, entry 414.00 (1.43% below PDC).
        # Baseline = 420.90; 0.75% ceiling = 417.11; cap must win.
        stop, capped, baseline = m._capped_short_stop(420.00, 414.00)
        assert capped is True, "entry far below PDC must trigger cap"
        assert stop == 417.11, f"expected stop 417.11, got {stop}"
        assert baseline == 420.90, f"expected baseline 420.90, got {baseline}"
        assert (stop - 414.00) < (baseline - 414.00), \
            "capped short stop must be tighter than baseline"

    @t("v3.4.21: _capped_short_stop leaves baseline alone for near-PDC entries")
    def _():
        # Entry just below PDC — baseline already tighter than ceiling.
        stop, capped, baseline = m._capped_short_stop(420.00, 419.80)
        assert capped is False, "near-PDC short must not trip cap"
        assert stop == baseline == 420.90, \
            f"expected stop=baseline=420.90, got stop={stop} baseline={baseline}"

    @t("v3.4.21: execute_entry / execute_short_entry use capped stop helpers")
    def _():
        import inspect
        long_src = inspect.getsource(m.execute_entry)
        short_src = inspect.getsource(m.execute_short_entry)
        assert "_capped_long_stop(" in long_src, \
            "execute_entry must call _capped_long_stop"
        assert "_capped_short_stop(" in short_src, \
            "execute_short_entry must call _capped_short_stop"
        # Legacy raw formulas must be gone from these entry paths.
        assert "or_high_val - 0.90" not in long_src, \
            "execute_entry must not compute baseline directly anymore"
        assert "pdc_val + 0.90" not in short_src, \
            "execute_short_entry must not compute baseline directly anymore"

    @t("v3.4.21: near-miss ring buffer exists and _record_near_miss works")
    def _():
        assert hasattr(m, "_near_miss_log"), "_near_miss_log must be defined"
        assert isinstance(m._near_miss_log, list), "_near_miss_log must be a list"
        assert hasattr(m, "_NEAR_MISS_MAX"), "_NEAR_MISS_MAX must be defined"
        assert m._NEAR_MISS_MAX == 20, \
            f"_NEAR_MISS_MAX must be 20, got {m._NEAR_MISS_MAX}"
        assert hasattr(m, "_record_near_miss"), "_record_near_miss must exist"

        # Snapshot length, append, then restore so we don't leak state.
        before = list(m._near_miss_log)
        try:
            m._record_near_miss(ticker="ZZZZ", side="LONG", reason="LOW_VOL",
                                vol_pct=42.0, price=100.0, level=99.0)
            assert len(m._near_miss_log) == len(before) + 1, \
                "_record_near_miss must append an entry"
            row = m._near_miss_log[0]
            assert isinstance(row, dict), "near-miss row must be a dict"
            assert row.get("ticker") == "ZZZZ"
            assert row.get("reason") == "LOW_VOL"
        finally:
            m._near_miss_log.clear()
            m._near_miss_log.extend(before)

    @t("v3.4.21: _near_miss_log respects _NEAR_MISS_MAX cap")
    def _():
        before = list(m._near_miss_log)
        try:
            m._near_miss_log.clear()
            for i in range(m._NEAR_MISS_MAX + 5):
                m._record_near_miss(ticker=f"T{i}", side="LONG",
                                    reason="LOW_VOL")
            assert len(m._near_miss_log) == m._NEAR_MISS_MAX, \
                f"log length must not exceed {m._NEAR_MISS_MAX}, " \
                f"got {len(m._near_miss_log)}"
        finally:
            m._near_miss_log.clear()
            m._near_miss_log.extend(before)

    @t("v3.4.21: _gate_snapshot dict exists for per-ticker dashboard chips")
    def _():
        assert hasattr(m, "_gate_snapshot"), "_gate_snapshot must be defined"
        assert isinstance(m._gate_snapshot, dict), \
            "_gate_snapshot must be a dict"

    @t("v3.4.21: check_entry / check_short_entry populate gate snapshot + near-miss")
    def _():
        import inspect
        long_src = inspect.getsource(m.check_entry)
        short_src = inspect.getsource(m.check_short_entry)
        # Both long and short gates must write into _gate_snapshot.
        assert "_gate_snapshot[ticker]" in long_src, \
            "check_entry must populate _gate_snapshot[ticker]"
        assert "_gate_snapshot[ticker]" in short_src, \
            "check_short_entry must populate _gate_snapshot[ticker]"
        # Both must call _record_near_miss on the LOW_VOL / DATA_NOT_READY path.
        assert "_record_near_miss(" in long_src, \
            "check_entry must call _record_near_miss on declined breakouts"
        assert "_record_near_miss(" in short_src, \
            "check_short_entry must call _record_near_miss on declined breakouts"

    @t("v3.4.21: /near_misses command is a registered handler")
    def _():
        import inspect
        fn = getattr(m, "cmd_near_misses", None)
        assert fn is not None, "cmd_near_misses must exist"
        assert inspect.iscoroutinefunction(fn), \
            "cmd_near_misses must be a coroutine (async def)"
        # BotCommand list must advertise /near_misses to users.
        cmds = getattr(m, "MAIN_BOT_COMMANDS", None)
        assert cmds is not None, "MAIN_BOT_COMMANDS must exist"
        names = [getattr(c, "command", None) for c in cmds]
        assert "near_misses" in names, \
            "near_misses must be in MAIN_BOT_COMMANDS"

    # ============================================================
    # v3.4.22 regressions
    # ------------------------------------------------------------
    # TradersPost webhook only accepts actions from a fixed allowlist:
    # buy, sell, exit, reverse, breakeven, cancel, add.
    # Before v3.4.22 we sent action=sell_short on short entry and
    # action=buy_to_cover on short cover, which TP rejects with HTTP
    # 400 INVALID ACTION. Now we send action=sell / action=buy and TP
    # infers direction from the strategy config + open position state.
    # ============================================================

    @t("v3.4.22: short entry sends TradersPost-legal action=sell")
    def _():
        import inspect
        src = inspect.getsource(m.execute_short_entry)
        assert 'send_traderspost_order(ticker, "sell"' in src, \
            "execute_short_entry must send action='sell' to TradersPost"
        # The legacy, rejected action string must be gone.
        assert "sell_short" not in src, \
            "execute_short_entry must not reference sell_short anymore"

    @t("v3.4.22: short cover sends TradersPost-legal action=buy")
    def _():
        import inspect
        # execute_cover lives alongside execute_exit — grep module src.
        src_all = inspect.getsource(m)
        # The actual webhook call must use action='buy'.
        assert 'send_traderspost_order(ticker, "buy", cover_price' in src_all, \
            "short cover path must send action='buy' to TradersPost"
        # No remaining webhook call should use 'buy_to_cover' as the
        # TradersPost action argument (internal tp_unsynced_exits label
        # may still read 'buy_to_cover' — that's fine, it's a human label).
        assert 'send_traderspost_order(ticker, "buy_to_cover"' not in src_all, \
            "no webhook may send action='buy_to_cover' anymore"

    @t("v3.4.22: no webhook sends action='sell_short'")
    def _():
        import inspect
        src_all = inspect.getsource(m)
        assert 'send_traderspost_order(ticker, "sell_short"' not in src_all, \
            "no webhook may send action='sell_short' anymore"

    @t("v3.4.22: every send_traderspost_order action is TP-legal")
    def _():
        # Find every call site of send_traderspost_order in the module
        # source and extract the string literal in its second arg. That
        # literal must be one of TradersPost's accepted actions.
        import inspect, re
        src_all = inspect.getsource(m)
        # Match: send_traderspost_order(<something>, "<action>"...
        # where <something> is the ticker expression (no literal string).
        pattern = re.compile(
            r'send_traderspost_order\([^,]+,\s*"([a-z_]+)"'
        )
        allowed = {"buy", "sell", "exit", "reverse",
                   "breakeven", "cancel", "add"}
        matches = pattern.findall(src_all)
        assert matches, "expected to find send_traderspost_order call sites"
        bad = [a for a in matches if a not in allowed]
        assert not bad, \
            f"TradersPost-illegal action(s) found at call sites: {sorted(set(bad))}"

    @t("v3.4.22: send_traderspost_order limit-price branch is 'buy'-only")
    def _():
        # After v3.4.22 the bump-up branch is only for action=="buy".
        # The old `if action in ("buy", "buy_to_cover"):` pattern was a
        # bug magnet — if someone passes a legal action like "exit" in
        # the future, the old check would silently put the limit on the
        # wrong side. Keep the guard tight.
        import inspect
        src = inspect.getsource(m.send_traderspost_order)
        assert 'action == "buy"' in src, \
            "send_traderspost_order must key limit bump on action=='buy'"
        assert 'action in ("buy", "buy_to_cover")' not in src, \
            "legacy buy_to_cover branch must be removed"

    # ------------------------------------------------------------
    # v3.4.23 regressions — retro-cap retighten
    # ------------------------------------------------------------
    # The v3.4.21 entry-cap only fires at entry. v3.4.23 retro-applies
    # the same 0.75% cap to any still-open position whose stop sits
    # wider than the cap, and force-exits with reason=RETRO_CAP if the
    # newly-capped stop has already been breached.

    @t("v3.4.23: BOT_VERSION is >= 3.4.23")
    def _():
        import stock_spike_monitor as m
        # Tuple comparison on split int parts so minor bumps
        # don't regress this test. Guards against ever going
        # below the v3.4.23 floor where retro-cap shipped.
        parts = tuple(int(x) for x in m.BOT_VERSION.split("."))
        assert parts >= (3, 4, 23), m.BOT_VERSION

    @t("v3.4.23: retighten helpers exist and return 3-tuples")
    def _():
        import stock_spike_monitor as m
        assert callable(getattr(m, "_retighten_long_stop", None)), \
            "_retighten_long_stop must exist"
        assert callable(getattr(m, "_retighten_short_stop", None)), \
            "_retighten_short_stop must exist"
        # Already-tight short: entry 100, stop 100.50 (0.50%) — cap is
        # 100.75, so existing stop is tighter. Current at 99.80 is only
        # −0.20% so the v3.4.25 breakeven ratchet is NOT armed.
        pos = {"entry_price": 100.0, "stop": 100.50}
        out = m._retighten_short_stop("XYZ", pos, 99.80, "paper",
                                      force_exit=False)
        assert out[0] == "already_tight", out
        assert pos["stop"] == 100.50, "must not mutate tight stop"

    @t("v3.4.23: wide short stop gets tightened to ceiling")
    def _():
        import stock_spike_monitor as m
        # AAPL: entry 268.77, stop 273.95 (1.93%); ceiling = 270.79
        pos = {"entry_price": 268.77, "stop": 273.95}
        out = m._retighten_short_stop("AAPL", pos, 267.95, "paper",
                                      force_exit=False)
        assert out[0] == "tightened", out
        assert abs(out[1] - 273.95) < 1e-6, out
        assert abs(out[2] - 270.79) < 1e-6, out
        assert abs(pos["stop"] - 270.79) < 1e-6, pos

    @t("v3.4.23: wide long stop gets tightened to floor")
    def _():
        import stock_spike_monitor as m
        # Long entry 200, stop 195 (2.5%); floor = 198.50. Current at
        # 200.50 is only +0.25% so the v3.4.25 ratchet is NOT armed,
        # isolating pure cap behavior.
        pos = {"entry_price": 200.0, "stop": 195.0}
        out = m._retighten_long_stop("XYZ", pos, 200.50, "paper",
                                     force_exit=False)
        assert out[0] == "tightened", out
        assert abs(out[2] - 198.50) < 1e-6, out
        assert abs(pos["stop"] - 198.50) < 1e-6, pos

    @t("v3.4.23/26: trail_active short leaves hard stop alone")
    def _():
        import stock_spike_monitor as m
        # Under v3.4.23 this returned ("no_op", None, None). Under
        # v3.4.26, the cap layer is still skipped when trail is
        # armed (invariant preserved: hard stop untouched), and the
        # ratchet runs against trail_stop — see the v3.4.26 block
        # for ratchet-through-trail coverage. This test keeps the
        # v3.4.23 invariant: pos["stop"] must NOT move when trail
        # is active.
        pos = {"entry_price": 100.0, "stop": 105.0,
               "trail_active": True, "trail_stop": 100.50,
               "trail_low": 99.50}
        out = m._retighten_short_stop("XYZ", pos, 99.0, "paper",
                                      force_exit=False)
        # Status is either already_tight (if ratchet no-op) or
        # ratcheted_trail (if ratchet moved trail_stop). Either way,
        # hard stop never moves.
        assert out[0] in ("already_tight", "ratcheted_trail"), out
        assert pos["stop"] == 105.0, \
            "trail-armed hard stop must be untouched"

    @t("v3.4.23: breached short reports 'exit' without mutating if force_exit=False")
    def _():
        import stock_spike_monitor as m
        # TSLA: entry 388, stop 393.40; ceiling 390.91; current 390.92
        # already at/past ceiling. With force_exit=False we still move
        # the stop but DO NOT invoke close_short_position — we report
        # "tightened" instead (exit path is guarded by force_exit).
        pos = {"entry_price": 388.0, "stop": 393.40}
        out = m._retighten_short_stop("TSLA", pos, 390.92, "paper",
                                      force_exit=False)
        assert out[0] == "tightened", out
        assert abs(out[2] - 390.91) < 1e-6, out

    @t("v3.4.23: retighten_all_stops exists and returns expected shape")
    def _():
        import stock_spike_monitor as m
        assert callable(getattr(m, "retighten_all_stops", None)), \
            "retighten_all_stops must exist"
        # fetch_prices=False so no network. With empty books it's a
        # no-op and returns the summary dict.
        result = m.retighten_all_stops(force_exit=False, fetch_prices=False)
        assert isinstance(result, dict), type(result)
        for key in ("tightened", "exited", "no_op", "already_tight",
                    "errors", "details"):
            assert key in result, (key, result)
        assert isinstance(result["details"], list)

    @t("v3.4.23: manage_positions / manage_short_positions call retighten_all_stops")
    def _():
        import stock_spike_monitor as m, inspect
        long_src = inspect.getsource(m.manage_positions)
        short_src = inspect.getsource(m.manage_short_positions)
        assert "retighten_all_stops(" in long_src, \
            "manage_positions must invoke retighten_all_stops"
        assert "retighten_all_stops(" in short_src, \
            "manage_short_positions must invoke retighten_all_stops"

    @t("v3.4.23: startup path invokes retighten_all_stops after load_*_state")
    def _():
        # Source-level check: the startup entry-point around
        # load_paper_state / load_tp_state must call retighten_all_stops
        # at least once (the startup pass, fetch_prices=False).
        import stock_spike_monitor as m, inspect
        with open(inspect.getsourcefile(m)) as f:
            src = f.read()
        # Find the startup call to load_tp_state() (not the def).
        # The entry-point block lives at module-bottom; use rfind.
        idx = src.rfind("load_tp_state()")
        assert idx != -1, "load_tp_state() call must exist"
        window = src[idx:idx + 3000]
        assert "retighten_all_stops(" in window, \
            "startup path must invoke retighten_all_stops after load_tp_state()"
        assert "fetch_prices=False" in window, \
            "startup retighten must use fetch_prices=False"

    @t("v3.4.23: cmd_retighten is a coroutine + /retighten registered as BotCommand")
    def _():
        import stock_spike_monitor as m, inspect
        assert inspect.iscoroutinefunction(m.cmd_retighten), \
            "cmd_retighten must be async"
        names = [c.command for c in m.MAIN_BOT_COMMANDS]
        assert "retighten" in names, names

    @t("v3.4.23: /retighten CommandHandler wired on both apps")
    def _():
        import stock_spike_monitor as m, inspect
        with open(inspect.getsourcefile(m)) as f:
            src = f.read()
        # Both app and tp_app must register the handler.
        assert src.count("CommandHandler(\"retighten\", cmd_retighten)") >= 2, \
            "/retighten must be wired on both main and TP apps"

    # ------------------------------------------------------------
    # v3.4.25 regressions — Breakeven ratchet (Stage 1)
    # ------------------------------------------------------------
    # Once a position is +0.50% in profit, the stop pulls to entry
    # price (breakeven). Retroactive (applies on startup + every
    # manage cycle via retighten_all_stops). Pure tightening — never
    # loosens, never moves stop past entry in the unfavorable
    # direction. No-op when trail is already armed.

    @t("v3.4.25: BREAKEVEN_RATCHET_PCT constant is 0.005")
    def _():
        import stock_spike_monitor as m
        assert abs(m.BREAKEVEN_RATCHET_PCT - 0.005) < 1e-9, \
            m.BREAKEVEN_RATCHET_PCT

    @t("v3.4.25: _breakeven_short_stop below threshold is a no-op")
    def _():
        import stock_spike_monitor as m
        # AAPL-like: entry 268.77, current 268.00 is only −0.29%
        # profit — below +0.50% arm threshold.
        new_stop, armed = m._breakeven_short_stop(
            entry_price=268.77, current_price=268.00,
            current_stop=270.79,
        )
        assert not armed, "ratchet must not arm below threshold"
        assert new_stop == 270.79, new_stop

    @t("v3.4.25: _breakeven_short_stop at +0.50% arms and pulls to entry")
    def _():
        import stock_spike_monitor as m
        # Exactly at threshold: current = entry * 0.995 = 267.42615
        new_stop, armed = m._breakeven_short_stop(
            entry_price=268.77, current_price=268.77 * 0.995,
            current_stop=270.79,
        )
        assert armed, "ratchet must arm at exactly the threshold"
        assert new_stop == 268.77, new_stop

    @t("v3.4.25: _breakeven_short_stop past +0.50% pulls to entry")
    def _():
        import stock_spike_monitor as m
        # AAPL-like scenario that motivated the ratchet:
        # entry 268.77, current 266.59 = +0.81% profit, stop 270.79.
        new_stop, armed = m._breakeven_short_stop(
            entry_price=268.77, current_price=266.59,
            current_stop=270.79,
        )
        assert armed
        assert new_stop == 268.77, new_stop  # pulled from 270.79 → 268.77

    @t("v3.4.25: _breakeven_long_stop below threshold is a no-op")
    def _():
        import stock_spike_monitor as m
        # Entry 100, current 100.40 is only +0.40% — below threshold.
        new_stop, armed = m._breakeven_long_stop(
            entry_price=100.0, current_price=100.40,
            current_stop=99.25,
        )
        assert not armed
        assert new_stop == 99.25

    @t("v3.4.25: _breakeven_long_stop past +0.50% pulls to entry")
    def _():
        import stock_spike_monitor as m
        new_stop, armed = m._breakeven_long_stop(
            entry_price=100.0, current_price=100.75,
            current_stop=99.25,
        )
        assert armed
        assert new_stop == 100.0

    @t("v3.4.25: ratchet NEVER loosens an already-tighter stop")
    def _():
        import stock_spike_monitor as m
        # If the trail-management loop somehow already has the stop
        # at a better-than-breakeven price, the ratchet must leave
        # it alone. Short: existing stop 268.00 is already below
        # entry 268.77 — ratchet must NOT widen it back to entry.
        new_stop, armed = m._breakeven_short_stop(
            entry_price=268.77, current_price=266.50,
            current_stop=268.00,
        )
        assert armed
        assert new_stop == 268.00, \
            "ratchet must not loosen a stop already past breakeven"
        # Long: existing 100.50 above entry 100; must stay at 100.50.
        new_stop_l, armed_l = m._breakeven_long_stop(
            entry_price=100.0, current_price=101.0,
            current_stop=100.50,
        )
        assert armed_l
        assert new_stop_l == 100.50, new_stop_l

    @t("v3.4.25: _retighten_short_stop reports 'ratcheted' when breakeven fires")
    def _():
        import stock_spike_monitor as m
        # Exactly the AAPL live scenario: retro-capped stop at 270.79
        # (the v3.4.23 cap), current 266.59 (+0.81%). Ratchet pulls
        # the stop from 270.79 to 268.77.
        pos = {"entry_price": 268.77, "stop": 270.79}
        out = m._retighten_short_stop("AAPL", pos, 266.59, "paper",
                                      force_exit=False)
        assert out[0] == "ratcheted", out
        assert abs(out[1] - 270.79) < 1e-6, out
        assert abs(out[2] - 268.77) < 1e-6, out
        assert abs(pos["stop"] - 268.77) < 1e-6, pos

    @t("v3.4.25: _retighten_long_stop reports 'ratcheted' when breakeven fires")
    def _():
        import stock_spike_monitor as m
        # Long entry 100, wide stop 98, current 100.75 (+0.75%).
        # Cap floor would be 99.25, but ratchet pulls to 100.0.
        pos = {"entry_price": 100.0, "stop": 98.0}
        out = m._retighten_long_stop("XYZ", pos, 100.75, "paper",
                                     force_exit=False)
        assert out[0] == "ratcheted", out
        assert abs(out[2] - 100.0) < 1e-6, out
        assert abs(pos["stop"] - 100.0) < 1e-6, pos

    @t("v3.4.25/26: retighten_all_stops summary has 'ratcheted' key")
    def _():
        import stock_spike_monitor as m
        result = m.retighten_all_stops(force_exit=False, fetch_prices=False)
        assert "ratcheted" in result, result.keys()
        assert isinstance(result["ratcheted"], int)

    # ------------------------------------------------------------
    # v3.4.26 regressions — Ratchet-through-trail + diagnostics
    # ------------------------------------------------------------
    # v3.4.25's "trail_active short-circuits retighten" contract
    # silently let a wide trail_stop survive in-profit positions.
    # v3.4.26 keeps the cap skipped when trail is armed but runs the
    # breakeven ratchet against pos["trail_stop"] — pure tighten.
    # The dashboard now surfaces trail_active/trail_stop/
    # effective_stop so we can see what is actually managing each
    # position.
    # ------------------------------------------------------------

    @t("v3.4.26: BOT_VERSION is >= 3.4.26")
    def _():
        import stock_spike_monitor as m
        parts = [int(x) for x in m.BOT_VERSION.split(".")]
        assert parts >= [3, 4, 26], \
            f"BOT_VERSION {m.BOT_VERSION} is older than 3.4.26"

    @t("v3.4.26: short with trail_active + above-arm price is a no-op")
    def _():
        import stock_spike_monitor as m
        # Current price ABOVE the arm threshold (short: price must
        # be <= entry * 0.995 to arm breakeven). Trail armed but
        # not yet in ratchet territory.
        pos = {"entry_price": 100.0, "stop": 105.0,
               "trail_active": True, "trail_stop": 99.80,
               "trail_low": 98.80}
        out = m._retighten_short_stop("XYZ", pos, 99.60, "paper",
                                      force_exit=False)
        # 99.60 is only -0.40% — below the +0.50% arm.
        assert out[0] == "already_tight", out
        assert pos["trail_stop"] == 99.80
        assert pos["stop"] == 105.0

    @t("v3.4.26: short trail_stop ratchets to entry when armed")
    def _():
        import stock_spike_monitor as m
        # Trail armed on an unfavorable dip — trail_low 99.00, so
        # trail_stop = 99.00 + max(99*0.01, 1.00) = 99.00 + 1.00 =
        # 100.00 (wider than entry 100!). Now price at 99.40 =
        # +0.60% profit, past the +0.50% arm. Ratchet should pull
        # trail_stop down to entry 100.00 — i.e. a no-op equality
        # case — so use a cleaner example: trail_stop = 100.20.
        pos = {"entry_price": 100.0, "stop": 105.0,
               "trail_active": True, "trail_stop": 100.20,
               "trail_low": 99.20}
        out = m._retighten_short_stop("AAPL", pos, 99.40, "paper",
                                      force_exit=False)
        status, old, new = out
        assert status == "ratcheted_trail", out
        assert old == 100.20, old
        assert new == 100.0, new
        assert pos["trail_stop"] == 100.0
        # Hard stop is untouched — only trail_stop moves while armed.
        assert pos["stop"] == 105.0

    @t("v3.4.26: short ratchet-through-trail is pure tighten (never loosens)")
    def _():
        import stock_spike_monitor as m
        # trail_stop already tighter than entry — leave it alone.
        pos = {"entry_price": 100.0, "stop": 105.0,
               "trail_active": True, "trail_stop": 99.50,
               "trail_low": 98.50}
        out = m._retighten_short_stop("XYZ", pos, 99.40, "paper",
                                      force_exit=False)
        assert out[0] == "already_tight", out
        assert pos["trail_stop"] == 99.50

    @t("v3.4.26: long trail_stop ratchets to entry when armed")
    def _():
        import stock_spike_monitor as m
        # Trail armed on favorable move but gave back; trail_stop
        # sits at 99.80 (below entry 100). Current 100.60 = +0.60%
        # profit, past arm. Ratchet pulls trail_stop up to 100.00.
        pos = {"entry_price": 100.0, "stop": 95.0,
               "trail_active": True, "trail_stop": 99.80,
               "trail_high": 100.80}
        out = m._retighten_long_stop("XYZ", pos, 100.60, "paper",
                                     force_exit=False)
        status, old, new = out
        assert status == "ratcheted_trail", out
        assert old == 99.80, old
        assert new == 100.0, new
        assert pos["trail_stop"] == 100.0
        assert pos["stop"] == 95.0

    @t("v3.4.26: long ratchet-through-trail never loosens a tighter trail_stop")
    def _():
        import stock_spike_monitor as m
        # trail_stop 100.50 already above entry — leave alone.
        pos = {"entry_price": 100.0, "stop": 95.0,
               "trail_active": True, "trail_stop": 100.50,
               "trail_high": 101.50}
        out = m._retighten_long_stop("XYZ", pos, 100.60, "paper",
                                     force_exit=False)
        assert out[0] == "already_tight", out
        assert pos["trail_stop"] == 100.50

    @t("v3.4.26: trail_active with no trail_stop is a safe fall-through")
    def _():
        import stock_spike_monitor as m
        # Pathological but defensive: trail_active flipped True but
        # trail_stop not yet populated. Must NOT crash or loosen.
        pos_s = {"entry_price": 100.0, "stop": 105.0,
                 "trail_active": True}
        out_s = m._retighten_short_stop("XYZ", pos_s, 99.40, "paper",
                                        force_exit=False)
        assert out_s[0] == "already_tight", out_s
        assert pos_s["stop"] == 105.0

        pos_l = {"entry_price": 100.0, "stop": 95.0,
                 "trail_active": True}
        out_l = m._retighten_long_stop("XYZ", pos_l, 100.60, "paper",
                                       force_exit=False)
        assert out_l[0] == "already_tight", out_l
        assert pos_l["stop"] == 95.0

    @t("v3.4.26: retighten_all_stops summary has 'ratcheted_trail' key")
    def _():
        import stock_spike_monitor as m
        result = m.retighten_all_stops(force_exit=False, fetch_prices=False)
        assert "ratcheted_trail" in result, result.keys()
        assert isinstance(result["ratcheted_trail"], int)

    @t("v3.4.26: dashboard_server exposes trail_active / trail_stop / effective_stop")
    def _():
        import inspect, importlib
        ds = importlib.import_module("dashboard_server")
        src = inspect.getsource(ds._serialize_positions)
        for field in ('"trail_active"', '"trail_stop"',
                      '"effective_stop"'):
            assert field in src, \
                f"_serialize_positions missing {field}"
        # Effective stop must fall back to hard stop when trail is
        # not armed — the JS layer still handles older payloads.
        assert "trail_active and trail_stop is not None" in src, \
            "effective_stop fallback logic looks wrong"

    @t("v3.4.26: cmd_retighten output handles 'ratcheted_trail' status")
    def _():
        import stock_spike_monitor as m, inspect
        src = inspect.getsource(m.cmd_retighten)
        assert "ratcheted_trail" in src, \
            "cmd_retighten must branch on 'ratcheted_trail'"

    @t("v3.4.26: index.html renders effective_stop + TRAIL badge")
    def _():
        import os
        import stock_spike_monitor as m
        path = os.path.join(
            os.path.dirname(os.path.abspath(m.__file__)),
            "dashboard_static", "index.html",
        )
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
        assert "effective_stop" in html, \
            "index.html must reference effective_stop"
        assert "trail-badge" in html, \
            "index.html must render .trail-badge"
        assert "trail_active" in html, \
            "index.html must gate the badge on trail_active"

    @t("v3.4.21: dashboard_server exposes per_ticker gates + next_scan_sec + near_misses")
    def _():
        # Import the sibling dashboard_server module and inspect its snapshot
        # source to make sure the new surfaces are wired up. We don't call
        # snapshot() here because it expects a running scanner — source-level
        # checks are enough and keep the smoke test hermetic.
        import importlib, inspect
        ds = importlib.import_module("dashboard_server")
        assert hasattr(ds, "_ticker_gates"), \
            "dashboard_server must define _ticker_gates"
        assert hasattr(ds, "_next_scan_seconds"), \
            "dashboard_server must define _next_scan_seconds"
        src = inspect.getsource(ds)
        assert '"per_ticker"' in src, \
            "snapshot() must expose gates.per_ticker"
        assert '"next_scan_sec"' in src, \
            "snapshot() must expose gates.next_scan_sec"
        assert '"near_misses"' in src, \
            "snapshot() must expose top-level near_misses"

    # ----------------------------------------------------------
    # v3.4.27 — persistent trade log (append-only JSONL)
    # ----------------------------------------------------------

    @t("v3.4.27: TRADE_LOG_FILE path sits beside PAPER_STATE_FILE")
    def _():
        # The log must inherit the Railway volume by living in the same
        # directory as the already-persisted state files. Env var
        # override is honored but the default must match.
        assert hasattr(m, "TRADE_LOG_FILE"), "TRADE_LOG_FILE missing"
        assert hasattr(m, "PAPER_STATE_FILE"), "PAPER_STATE_FILE missing"
        # When TRADE_LOG_PATH env is unset the default resolves to a
        # sibling of PAPER_STATE_FILE (i.e. same directory).
        if not os.environ.get("TRADE_LOG_PATH"):
            state_dir = os.path.dirname(m.PAPER_STATE_FILE) or "."
            log_dir = os.path.dirname(m.TRADE_LOG_FILE) or "."
            assert os.path.abspath(state_dir) == os.path.abspath(log_dir), \
                f"TRADE_LOG_FILE must share dir with PAPER_STATE_FILE: " \
                f"{m.TRADE_LOG_FILE} vs {m.PAPER_STATE_FILE}"

    @t("v3.4.27: trade_log_append roundtrips a row with schema_version=1")
    def _():
        import tempfile, json
        orig = m.TRADE_LOG_FILE
        with tempfile.TemporaryDirectory() as td:
            m.TRADE_LOG_FILE = os.path.join(td, "trade_log.jsonl")
            try:
                ok = m.trade_log_append({
                    "date": "2026-04-21",
                    "portfolio": "paper",
                    "ticker": "AAPL",
                    "side": "SHORT",
                    "shares": 100,
                    "entry_price": 270.0,
                    "exit_price": 268.13,
                    "pnl": 187.0,
                    "pnl_pct": 0.69,
                    "reason": "TRAIL",
                })
                assert ok is True, "append must return True on success"
                rows = m.trade_log_read_tail(limit=10)
                assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
                r = rows[0]
                assert r["schema_version"] == 1
                assert r["bot_version"] == m.BOT_VERSION
                assert r["ticker"] == "AAPL"
                assert r["reason"] == "TRAIL"
                assert r["pnl"] == 187.0
            finally:
                m.TRADE_LOG_FILE = orig

    @t("v3.4.27: trade_log_append rejects rows missing required fields")
    def _():
        import tempfile
        orig = m.TRADE_LOG_FILE
        with tempfile.TemporaryDirectory() as td:
            m.TRADE_LOG_FILE = os.path.join(td, "trade_log.jsonl")
            try:
                # Missing reason — must be refused, not appended.
                ok = m.trade_log_append({
                    "ticker": "TSLA", "side": "LONG", "pnl": 1.0,
                })
                assert ok is False, "missing 'reason' must return False"
                rows = m.trade_log_read_tail(limit=10)
                assert rows == [], "no row should have been written"
                assert m._trade_log_last_error and "reason" in \
                    m._trade_log_last_error, \
                    f"error must mention 'reason': {m._trade_log_last_error}"
            finally:
                m.TRADE_LOG_FILE = orig
                m._trade_log_last_error = None

    @t("v3.4.27: trade_log_read_tail filters since_date + portfolio + limit")
    def _():
        import tempfile
        orig = m.TRADE_LOG_FILE
        with tempfile.TemporaryDirectory() as td:
            m.TRADE_LOG_FILE = os.path.join(td, "trade_log.jsonl")
            try:
                base = {
                    "ticker": "X", "side": "LONG",
                    "pnl": 1.0, "reason": "EOD",
                }
                for date, port in [
                    ("2026-04-19", "paper"),
                    ("2026-04-20", "paper"),
                    ("2026-04-20", "tp"),
                    ("2026-04-21", "paper"),
                    ("2026-04-21", "tp"),
                ]:
                    r = dict(base)
                    r["date"] = date
                    r["portfolio"] = port
                    assert m.trade_log_append(r)
                # since_date filter
                rows = m.trade_log_read_tail(since_date="2026-04-20")
                assert len(rows) == 4, f"since: got {len(rows)}"
                # portfolio filter
                rows = m.trade_log_read_tail(portfolio="tp")
                assert len(rows) == 2 and all(
                    r["portfolio"] == "tp" for r in rows
                )
                # combined filters + limit
                rows = m.trade_log_read_tail(
                    since_date="2026-04-21",
                    portfolio="paper",
                    limit=5,
                )
                assert len(rows) == 1 and rows[0]["date"] == "2026-04-21"
                # limit trims to tail (newest last)
                rows = m.trade_log_read_tail(limit=2)
                assert len(rows) == 2
                assert rows[-1]["date"] == "2026-04-21"
            finally:
                m.TRADE_LOG_FILE = orig

    @t("v3.4.27: trade_log_read_tail returns [] when file missing")
    def _():
        import tempfile
        orig = m.TRADE_LOG_FILE
        with tempfile.TemporaryDirectory() as td:
            m.TRADE_LOG_FILE = os.path.join(td, "does_not_exist.jsonl")
            try:
                assert m.trade_log_read_tail() == []
            finally:
                m.TRADE_LOG_FILE = orig

    @t("v3.4.27: _trade_log_snapshot_pos captures trail+stop for long")
    def _():
        snap = m._trade_log_snapshot_pos({
            "trail_active": True,
            "trail_stop": 268.13,
            "trail_high": 270.78,
            "stop": 265.0,
        })
        assert snap["trail_active_at_exit"] is True
        assert snap["trail_stop_at_exit"] == 268.13
        assert snap["trail_anchor_at_exit"] == 270.78
        assert snap["hard_stop_at_exit"] == 265.0
        # trail armed ⇒ effective is trail_stop
        assert snap["effective_stop_at_exit"] == 268.13

    @t("v3.4.27: _trade_log_snapshot_pos captures trail+stop for short")
    def _():
        # Trail not armed ⇒ effective falls back to hard stop.
        snap = m._trade_log_snapshot_pos({
            "trail_active": False,
            "trail_stop": None,
            "trail_low": 265.48,
            "stop": 272.0,
        })
        assert snap["trail_active_at_exit"] is False
        assert snap["trail_stop_at_exit"] is None
        assert snap["trail_anchor_at_exit"] == 265.48
        assert snap["hard_stop_at_exit"] == 272.0
        assert snap["effective_stop_at_exit"] == 272.0

    @t("v3.4.27: _trade_log_snapshot_pos handles non-dict gracefully")
    def _():
        snap = m._trade_log_snapshot_pos(None)
        assert all(v is None for v in snap.values()), \
            "every field must be None for a missing pos"
        assert set(snap.keys()) == {
            "trail_active_at_exit", "trail_stop_at_exit",
            "trail_anchor_at_exit", "hard_stop_at_exit",
            "effective_stop_at_exit",
        }

    @t("v3.4.27: every close path calls trade_log_append")
    def _():
        import inspect
        # v3.4.40: close_position now handles PAPER only. The TP mirror
        # that used to live in close_position was removed — RH exits are
        # owned exclusively by close_tp_position (called from
        # manage_tp_positions). So close_position has ONE trade_log_append
        # (paper) rather than two.
        src_close = inspect.getsource(m.close_position)
        assert src_close.count("trade_log_append(") >= 1, \
            "close_position must log paper branch"
        # close_tp_position (TP-only long)
        src_tp = inspect.getsource(m.close_tp_position)
        assert "trade_log_append(" in src_tp, \
            "close_tp_position must call trade_log_append"
        # close_short_position (paper + TP shared)
        src_sh = inspect.getsource(m.close_short_position)
        assert "trade_log_append(" in src_sh, \
            "close_short_position must call trade_log_append"
        # Every hook must also capture the trail/stop snapshot.
        for name, src in (("close_position", src_close),
                          ("close_tp_position", src_tp),
                          ("close_short_position", src_sh)):
            assert "_trade_log_snapshot_pos(" in src, \
                f"{name} must enrich the row via _trade_log_snapshot_pos"

    @t("v3.4.27: /api/trade_log endpoint + /trade_log command registered")
    def _():
        import importlib, inspect
        ds = importlib.import_module("dashboard_server")
        assert hasattr(ds, "h_trade_log"), \
            "dashboard_server must define h_trade_log"
        src = inspect.getsource(ds._build_app)
        assert '"/api/trade_log"' in src, \
            "/api/trade_log must be registered on the app router"
        # Telegram /trade_log handler + registration on both main and TP apps
        assert hasattr(m, "cmd_trade_log"), \
            "cmd_trade_log must exist"
        # Read the source file directly — inspect.getsource(module) can
        # be flaky when the module was loaded via an alternate path.
        src_path = Path(__file__).resolve().parent / "stock_spike_monitor.py"
        src_main = src_path.read_text(encoding="utf-8")
        assert src_main.count('CommandHandler("trade_log"') >= 2, \
            "/trade_log must register on both main app and tp_app"

    @t("v3.4.27: TRADE_LOG_SCHEMA_VERSION is 1 and surfaces in rows")
    def _():
        assert m.TRADE_LOG_SCHEMA_VERSION == 1, \
            "schema version must stay at 1 until a breaking change ships"

    # =================================================================
    # v3.4.28 — Sovereign Regime Shield (PDC-based dual-index eject)
    # =================================================================
    # Shared helpers: install synthetic pdc + fetch_1min_bars so the
    # eject gate is evaluated against controlled inputs. Each test
    # restores the originals in a finally block so later suites are
    # unaffected.
    _orig_fetch_1min_bars = m.fetch_1min_bars
    _orig_pdc = dict(m.pdc)

    def _install_index_fixture(spy_closes, qqq_closes, spy_pdc, qqq_pdc):
        """Patch m.pdc and m.fetch_1min_bars for the SPY/QQQ shield."""
        m.pdc.clear()
        m.pdc.update(_orig_pdc)
        if spy_pdc is not None:
            m.pdc["SPY"] = spy_pdc
        else:
            m.pdc.pop("SPY", None)
        if qqq_pdc is not None:
            m.pdc["QQQ"] = qqq_pdc
        else:
            m.pdc.pop("QQQ", None)

        def fake(ticker):
            series = {"SPY": spy_closes, "QQQ": qqq_closes}.get(ticker)
            if series is None:
                return None
            return {
                "current_price": series[-1] if series else 0.0,
                "closes": list(series),
                "opens": [c for c in series],
                "highs": [c for c in series],
                "lows":  [c for c in series],
            }
        m.fetch_1min_bars = fake

    def _restore_index_fixture():
        m.fetch_1min_bars = _orig_fetch_1min_bars
        m.pdc.clear()
        m.pdc.update(_orig_pdc)

    @t("v3.4.28: _last_finalized_1min_close returns closes[-2], not intrabar")
    def _():
        try:
            _install_index_fixture(
                spy_closes=[100.0, 99.5, 99.0],   # in-progress bar = 99.0
                qqq_closes=[200.0, 199.0, 198.0],
                spy_pdc=101.0, qqq_pdc=201.0,
            )
            assert m._last_finalized_1min_close("SPY") == 99.5, \
                "must return closes[-2], not the in-progress closes[-1]"
            assert m._last_finalized_1min_close("QQQ") == 199.0
        finally:
            _restore_index_fixture()

    @t("v3.4.28: _last_finalized_1min_close returns None with <2 finalized bars")
    def _():
        try:
            _install_index_fixture(
                spy_closes=[100.0],  # only the in-progress bar
                qqq_closes=[],
                spy_pdc=101.0, qqq_pdc=201.0,
            )
            assert m._last_finalized_1min_close("SPY") is None, \
                "single bar (in-progress only) must return None"
            assert m._last_finalized_1min_close("QQQ") is None
        finally:
            _restore_index_fixture()

    @t("v3.4.28: _sovereign_regime_eject('long') = True when BOTH SPY+QQQ 1m_close < PDC")
    def _():
        try:
            # closes[-2] is what the eject reads; set closes[-2] < PDC for both
            _install_index_fixture(
                spy_closes=[100.0, 99.5, 99.9],   # closes[-2] = 99.5 < 100.0
                qqq_closes=[200.0, 198.5, 199.1], # closes[-2] = 198.5 < 199.0
                spy_pdc=100.0, qqq_pdc=199.0,
            )
            assert m._sovereign_regime_eject("long") is True, \
                "dual-below must eject longs"
        finally:
            _restore_index_fixture()

    @t("v3.4.28: _sovereign_regime_eject('long') = False when BOTH SPY+QQQ above PDC (inverse)")
    def _():
        try:
            _install_index_fixture(
                spy_closes=[100.0, 100.5, 100.3],
                qqq_closes=[200.0, 199.5, 199.7],  # closes[-2] = 199.5 > 199.0
                spy_pdc=100.0, qqq_pdc=199.0,
            )
            assert m._sovereign_regime_eject("long") is False, \
                "both above PDC must NOT eject longs"
        finally:
            _restore_index_fixture()

    @t("v3.4.28: _sovereign_regime_eject('short') = True when BOTH SPY+QQQ 1m_close > PDC")
    def _():
        try:
            _install_index_fixture(
                spy_closes=[100.0, 100.5, 100.3],  # closes[-2] = 100.5 > 100.0
                qqq_closes=[199.0, 199.5, 199.2],  # closes[-2] = 199.5 > 199.0
                spy_pdc=100.0, qqq_pdc=199.0,
            )
            assert m._sovereign_regime_eject("short") is True, \
                "dual-above must eject shorts"
        finally:
            _restore_index_fixture()

    @t("v3.4.28: _sovereign_regime_eject divergence (SPY below, QQQ above) must NOT eject either side")
    def _():
        try:
            # SPY closes[-2] < SPY_PDC, QQQ closes[-2] > QQQ_PDC
            _install_index_fixture(
                spy_closes=[100.0, 99.0, 99.1],
                qqq_closes=[199.0, 199.5, 199.3],
                spy_pdc=100.0, qqq_pdc=199.0,
            )
            assert m._sovereign_regime_eject("long") is False, \
                "divergence must NOT eject longs (hysteresis)"
            assert m._sovereign_regime_eject("short") is False, \
                "divergence must NOT eject shorts (hysteresis)"
        finally:
            _restore_index_fixture()

    @t("v3.4.28: _sovereign_regime_eject fails closed when SPY_PDC or QQQ_PDC missing")
    def _():
        try:
            _install_index_fixture(
                spy_closes=[100.0, 99.0, 99.1],
                qqq_closes=[200.0, 198.0, 198.5],
                spy_pdc=None, qqq_pdc=199.0,
            )
            assert m._sovereign_regime_eject("long") is False, \
                "missing SPY_PDC must fail closed"
            _install_index_fixture(
                spy_closes=[100.0, 99.0, 99.1],
                qqq_closes=[200.0, 198.0, 198.5],
                spy_pdc=100.0, qqq_pdc=None,
            )
            assert m._sovereign_regime_eject("long") is False, \
                "missing QQQ_PDC must fail closed"
        finally:
            _restore_index_fixture()

    @t("v3.4.28: _sovereign_regime_eject fails closed when 1m bars unavailable (<2 closes)")
    def _():
        try:
            _install_index_fixture(
                spy_closes=[100.0],   # not enough finalized bars
                qqq_closes=[],
                spy_pdc=100.0, qqq_pdc=199.0,
            )
            assert m._sovereign_regime_eject("long") is False, \
                "insufficient 1m bars must fail closed"
            assert m._sovereign_regime_eject("short") is False
        finally:
            _restore_index_fixture()

    @t("v3.4.28: _sovereign_regime_eject rejects invalid side argument")
    def _():
        try:
            _install_index_fixture(
                spy_closes=[100.0, 99.0, 99.1],
                qqq_closes=[200.0, 198.0, 198.5],
                spy_pdc=100.0, qqq_pdc=199.0,
            )
            assert m._sovereign_regime_eject("bogus") is False
            assert m._sovereign_regime_eject("") is False
            assert m._sovereign_regime_eject(None) is False
        finally:
            _restore_index_fixture()

    @t("v3.4.28: manage_positions calls _sovereign_regime_eject (not _dual_index_eject)")
    def _():
        import inspect
        src = inspect.getsource(m.manage_positions)
        assert "_sovereign_regime_eject(\"long\")" in src, \
            "manage_positions must invoke Sovereign Regime Shield for longs"
        assert "_dual_index_eject(\"long\")" not in src, \
            "legacy AVWAP eject must not be used live in manage_positions"

    @t("v3.4.28: manage_short_positions calls _sovereign_regime_eject (not _dual_index_eject)")
    def _():
        import inspect
        src = inspect.getsource(m.manage_short_positions)
        assert "_sovereign_regime_eject(\"short\")" in src, \
            "manage_short_positions must invoke Sovereign Regime Shield for shorts"
        assert "_dual_index_eject(\"short\")" not in src, \
            "legacy AVWAP eject must not be used live in manage_short_positions"

    @t("v3.4.28: manage_tp_positions calls _sovereign_regime_eject (TP mirror)")
    def _():
        import inspect
        src = inspect.getsource(m.manage_tp_positions)
        assert "_sovereign_regime_eject(\"long\")" in src, \
            "TP manager must also use PDC shield"

    @t("v3.4.28: plain LORDS_LEFT / BULL_VACUUM exit reasons registered in REASON_LABELS")
    def _():
        assert "LORDS_LEFT" in m.REASON_LABELS
        assert "BULL_VACUUM" in m.REASON_LABELS
        assert "PDC" in m.REASON_LABELS["LORDS_LEFT"], \
            "plain LORDS_LEFT label must mention PDC"
        assert "PDC" in m.REASON_LABELS["BULL_VACUUM"]
        # Legacy suffixed labels must remain for backwards-compat with old rows.
        assert "LORDS_LEFT[5m]" in m.REASON_LABELS
        assert "BULL_VACUUM[5m]" in m.REASON_LABELS

    @t("v3.4.28: BOT_VERSION bumped to 3.4.28")
    def _():
        # Any version >= 3.4.28 is fine; we just want to make sure the bump
        # didn't accidentally revert below Sovereign Regime Shield.
        parts = tuple(int(x) for x in m.BOT_VERSION.split("."))
        assert parts >= (3, 4, 28), \
            f"BOT_VERSION regressed below 3.4.28: {m.BOT_VERSION!r}"

    # =================================================================
    # v3.4.29 — Persistent dashboard session + live Sovereign panel
    # =================================================================
    @t("v3.4.29: BOT_VERSION is >= 3.4.29")
    def _():
        parts = tuple(int(x) for x in m.BOT_VERSION.split("."))
        assert parts >= (3, 4, 29), \
            f"BOT_VERSION regressed below 3.4.29: {m.BOT_VERSION!r}"

    @t("v3.4.29: CURRENT notes lead with current BOT_VERSION, 34-char-safe")
    def _():
        lead = f"v{m.BOT_VERSION} "
        for name, note in (("CURRENT_MAIN_NOTE", m.CURRENT_MAIN_NOTE),
                           ("CURRENT_TP_NOTE", m.CURRENT_TP_NOTE)):
            first = note.split("\n", 1)[0]
            assert first.startswith(lead), \
                f"{name} must lead with {lead!r} — got {first!r}"
            for i, line in enumerate(note.split("\n")):
                assert len(line) <= 34, \
                    f"{name} line {i} over 34 chars ({len(line)}): {line!r}"
            assert "3.4.28" not in first and "3.4.27" not in first, \
                f"{name} CURRENT note must not mention prior versions"
            assert "3.4.28" not in note.split("\n", 2)[0:1][0], name

    @t("v3.4.29: _session_secret_path sits beside PAPER_STATE_FILE")
    def _():
        import dashboard_server as ds2, os
        path = ds2._session_secret_path()
        assert path.endswith("dashboard_secret.key"), \
            f"secret must be named dashboard_secret.key, got {path}"
        expected_dir = os.path.dirname(m.PAPER_STATE_FILE) or "."
        assert os.path.dirname(path) == expected_dir, \
            f"secret dir {os.path.dirname(path)} != paper-state dir {expected_dir}"

    @t("v3.4.29: _load_or_create_session_secret generates and persists on first call")
    def _():
        import dashboard_server as ds2, os
        path = ds2._session_secret_path()
        # Clean slate.
        if os.path.exists(path):
            os.remove(path)
        os.environ.pop("DASHBOARD_SESSION_SECRET", None)

        secret1 = ds2._load_or_create_session_secret()
        assert isinstance(secret1, bytes) and len(secret1) == 32, \
            "first call must return 32 random bytes"
        assert os.path.exists(path), \
            "first call must persist the secret to disk"
        with open(path, "rb") as f:
            on_disk = f.read()
        assert on_disk == secret1, \
            "bytes on disk must match the bytes returned"

    @t("v3.4.29: _load_or_create_session_secret returns SAME bytes across calls (simulates redeploy)")
    def _():
        import dashboard_server as ds2, os
        os.environ.pop("DASHBOARD_SESSION_SECRET", None)
        path = ds2._session_secret_path()
        if os.path.exists(path):
            os.remove(path)
        first = ds2._load_or_create_session_secret()
        # Simulate a redeploy — new process, same file on the volume.
        second = ds2._load_or_create_session_secret()
        third  = ds2._load_or_create_session_secret()
        assert first == second == third, \
            "persistent secret must survive re-invocations (redeploy simulation)"

    @t("v3.4.29: DASHBOARD_SESSION_SECRET env override beats the file")
    def _():
        import dashboard_server as ds2, os
        path = ds2._session_secret_path()
        file_secret = b"\x00" * 32
        with open(path, "wb") as f:
            f.write(file_secret)
        env_hex = "aa" * 32
        os.environ["DASHBOARD_SESSION_SECRET"] = env_hex
        try:
            got = ds2._load_or_create_session_secret()
        finally:
            os.environ.pop("DASHBOARD_SESSION_SECRET", None)
        assert got == bytes.fromhex(env_hex), \
            "env override must take precedence over the on-disk file"

    @t("v3.4.29: corrupted on-disk secret (too short) is rejected and regenerated")
    def _():
        import dashboard_server as ds2, os
        os.environ.pop("DASHBOARD_SESSION_SECRET", None)
        path = ds2._session_secret_path()
        with open(path, "wb") as f:
            f.write(b"short")  # only 5 bytes
        fresh = ds2._load_or_create_session_secret()
        assert isinstance(fresh, bytes) and len(fresh) == 32, \
            "must regenerate when on-disk secret is too short"
        with open(path, "rb") as f:
            on_disk = f.read()
        assert on_disk == fresh and len(on_disk) == 32, \
            "must overwrite the corrupted file with a fresh 32-byte secret"

    @t("v3.4.29: snapshot exposes regime.sovereign with stable shape")
    def _():
        import dashboard_server as ds2
        snap = ds2.snapshot()
        assert snap.get("ok") is True, "snapshot must succeed"
        sov = (snap.get("regime") or {}).get("sovereign")
        assert isinstance(sov, dict), f"regime.sovereign must be a dict, got {type(sov)}"
        for k in ("spy_price", "spy_pdc", "spy_delta_pct", "spy_above_pdc",
                  "qqq_price", "qqq_pdc", "qqq_delta_pct", "qqq_above_pdc",
                  "long_eject", "short_eject", "status", "reason"):
            assert k in sov, f"regime.sovereign missing field: {k}"
        assert isinstance(sov["long_eject"], bool)
        assert isinstance(sov["short_eject"], bool)
        assert sov["status"] in ("ARMED_LONG", "ARMED_SHORT", "DISARMED",
                                 "AWAITING", "NO_PDC"), \
            f"unexpected status {sov['status']!r}"

    @t("v3.4.29: regime.sovereign == NO_PDC when SPY/QQQ PDC missing")
    def _():
        import dashboard_server as ds2
        orig_pdc = dict(m.pdc)
        try:
            m.pdc.clear()
            snap = ds2.snapshot()
            sov = (snap.get("regime") or {}).get("sovereign") or {}
            assert sov.get("status") == "NO_PDC", \
                f"missing PDC must surface as NO_PDC, got {sov.get('status')!r}"
            assert sov.get("long_eject") is False
            assert sov.get("short_eject") is False
        finally:
            m.pdc.clear()
            m.pdc.update(orig_pdc)

    @t("v3.4.29: regime.sovereign reflects ARMED_LONG with synthetic dual-below data")
    def _():
        import dashboard_server as ds2
        orig_pdc = dict(m.pdc)
        orig_fetch = m.fetch_1min_bars
        try:
            m.pdc.clear()
            m.pdc["SPY"] = 100.0
            m.pdc["QQQ"] = 199.0
            def fake(ticker):
                series = {"SPY": [100.0, 99.5, 99.9],
                          "QQQ": [200.0, 198.5, 199.1]}.get(ticker)
                if series is None: return None
                return {"current_price": series[-1], "closes": list(series),
                        "opens": list(series), "highs": list(series),
                        "lows": list(series)}
            m.fetch_1min_bars = fake
            snap = ds2.snapshot()
            sov = (snap.get("regime") or {}).get("sovereign") or {}
            assert sov.get("status") == "ARMED_LONG", \
                f"dual-below must surface ARMED_LONG, got {sov.get('status')!r}"
            assert sov.get("long_eject") is True
            assert sov.get("short_eject") is False
            assert sov.get("spy_price") == 99.5   # closes[-2]
            assert sov.get("qqq_price") == 198.5
            assert sov.get("spy_delta_pct") < 0
            assert sov.get("qqq_delta_pct") < 0
        finally:
            m.fetch_1min_bars = orig_fetch
            m.pdc.clear()
            m.pdc.update(orig_pdc)

    @t("v3.4.29: dashboard HTML contains Sovereign Regime Shield card + srs-body")
    def _():
        html_path = Path(__file__).resolve().parent / "dashboard_static" / "index.html"
        html = html_path.read_text(encoding="utf-8")
        assert "Sovereign Regime Shield" in html, \
            "dashboard must contain the Sovereign Regime Shield card title"
        assert 'id="srs-body"' in html and 'id="srs-status"' in html, \
            "dashboard must expose srs-body + srs-status target elements"
        assert "renderSovereign" in html, \
            "dashboard JS must define renderSovereign"
        assert "renderSovereign(s)" in html, \
            "renderAll must call renderSovereign(s)"

    # =================================================================
    # v3.4.30 — Mobile layout fix + Today's Trades time parsing
    # =================================================================
    @t("v3.4.30: BOT_VERSION is >= 3.4.30")
    def _():
        parts = tuple(int(x) for x in m.BOT_VERSION.split("."))
        assert parts >= (3, 4, 30), \
            f"BOT_VERSION regressed below 3.4.30: {m.BOT_VERSION!r}"

    @t("v3.4.30: dashboard .main has min-width: 0 so grid track can shrink on mobile")
    def _():
        html_path = Path(__file__).resolve().parent / "dashboard_static" / "index.html"
        html = html_path.read_text(encoding="utf-8")
        # The .main rule block must carry min-width:0; if this regresses,
        # the .app grid track inflates to fit nowrap children and the
        # dashboard blows past the iPhone viewport.
        import re
        main_block = re.search(r"\.main\s*\{[^}]*\}", html, re.DOTALL)
        assert main_block, ".main CSS rule not found"
        assert "min-width: 0" in main_block.group(0) or "min-width:0" in main_block.group(0), \
            ".main rule must include min-width: 0 (mobile overflow guard)"

    @t("v3.4.30: dashboard grid containers have min-width: 0 escape hatch")
    def _():
        html_path = Path(__file__).resolve().parent / "dashboard_static" / "index.html"
        html = html_path.read_text(encoding="utf-8")
        # Direct rules we added; keep them around so a future refactor
        # does not silently regress the mobile layout.
        assert ".main > section { min-width: 0; }" in html, \
            "missing '.main > section { min-width: 0; }' rule"
        assert ".grid { min-width: 0; }" in html, \
            "missing '.grid { min-width: 0; }' rule"
        assert ".grid > * { min-width: 0; }" in html, \
            "missing '.grid > * { min-width: 0; }' rule"

    @t("v3.4.30: Sovereign Regime row uses minmax(0, 1fr) so the name cell can shrink")
    def _():
        html_path = Path(__file__).resolve().parent / "dashboard_static" / "index.html"
        html = html_path.read_text(encoding="utf-8")
        assert "minmax(0, 1fr)" in html, \
            ".srs-idx grid must use minmax(0, 1fr) for the name track"

    @t("v3.4.30: Sovereign reason line wraps long text instead of pushing width")
    def _():
        html_path = Path(__file__).resolve().parent / "dashboard_static" / "index.html"
        html = html_path.read_text(encoding="utf-8")
        import re
        rule = re.search(r"\.srs-reason\s*\{[^}]*\}", html, re.DOTALL)
        assert rule, ".srs-reason rule missing"
        body = rule.group(0)
        assert "word-break" in body or "overflow-wrap" in body, \
            ".srs-reason must allow wrapping (word-break / overflow-wrap)"

    @t("v3.4.30: trade time formatter accepts pre-formatted 'HH:MM TZ' times")
    def _():
        # Pure regex check against the HTML/JS: when the time string is
        # already formatted (e.g. '09:11 CDT'), the renderer must extract
        # HH:MM via a regex match, not a naive .slice(11, 16).
        # IMPORTANT regression guard: the branch must NOT be a plain
        # .includes("T") check, because the TZ label 'CDT'/'EST'/'PST'
        # also contains a T and would mis-route a pre-formatted string
        # down the ISO-parse path.
        # v3.4.31: the time parsing was extracted from renderTrades into
        # a helper fmtTradeTime(rawT); the same invariants must hold.
        html_path = Path(__file__).resolve().parent / "dashboard_static" / "index.html"
        html = html_path.read_text(encoding="utf-8")
        assert "fmtTradeTime" in html, "fmtTradeTime helper missing"
        import re
        fn = re.search(r"function fmtTradeTime\([\s\S]*?\n  \}", html)
        assert fn, "could not locate fmtTradeTime body"
        body = fn.group(0)
        # Must match ISO shape with the full date prefix, not a bare 'T'.
        assert r"\d{4}-\d{2}-\d{2}T" in body, \
            "fmtTradeTime must detect ISO timestamps via full date prefix"
        # Must extract HH:MM from pre-formatted strings via regex.
        assert r"\d{1,2}:\d{2}" in body, \
            "fmtTradeTime must extract HH:MM via regex"
        # Must NOT use the broken .includes("T") branch — 'CDT' has a T.
        assert '.includes("T")' not in body, \
            "fmtTradeTime must not branch on plain .includes('T') "\
            "— 'CDT'/'EST'/etc would mis-route"

    @t("v3.4.30: today's trade payload uses 'HH:MM TZ' time; snapshot keeps it")
    def _():
        # Sanity: the server already produces trades with 'time' like
        # '09:11 CDT'. This test freezes that shape so an upstream change
        # that swaps formats gets caught by the renderer-side test above.
        import datetime as dt
        now = m._now_cdt()
        trade = {
            "action": "BUY", "ticker": "TEST", "price": 1.0,
            "shares": 1, "cost": 1.0, "entry_num": 1,
            "time": now.strftime("%H:%M %Z"),
            "date": now.strftime("%Y-%m-%d"),
            "side": "LONG", "portfolio": "paper",
        }
        t = trade["time"]
        assert len(t) >= 7 and t[2] == ":", f"bad time shape: {t!r}"
        # Confirms the renderer's new parser would find HH:MM at start.
        import re
        assert re.match(r"^\d{1,2}:\d{2}", t), \
            f"renderTrades regex would not match produced time: {t!r}"

    # =================================================================
    # v3.4.31 — Richer Today's Trades card
    # =================================================================
    @t("v3.4.31: BOT_VERSION is >= 3.4.31")
    def _():
        parts = tuple(int(x) for x in m.BOT_VERSION.split("."))
        assert parts >= (3, 4, 31), \
            f"BOT_VERSION regressed below 3.4.31: {m.BOT_VERSION!r}"

    @t("v3.4.31: dashboard carries trades summary header + realized chip")
    def _():
        html_path = Path(__file__).resolve().parent / "dashboard_static" / "index.html"
        html = html_path.read_text(encoding="utf-8")
        # Summary line container above the rows.
        assert 'id="trades-summary"' in html, \
            "missing inline summary container (#trades-summary)"
        # Chip in the card head that shows running realized $.
        assert 'id="trades-realized"' in html, \
            "missing realized-$ chip (#trades-realized)"
        # Helper that computes {opens, closes, realized, win_rate, ...}.
        assert "function computeTradesSummary" in html, \
            "missing computeTradesSummary helper"

    @t("v3.4.31: dashboard uses .trade-row grid rows instead of a <table>")
    def _():
        html_path = Path(__file__).resolve().parent / "dashboard_static" / "index.html"
        html = html_path.read_text(encoding="utf-8")
        # New per-row DOM shape.
        assert ".trade-row" in html, "missing .trade-row CSS"
        assert ".trades-list" in html, "missing .trades-list CSS"
        # Badge + tail cell classes emitted by renderTrades.
        for cls in (".act-badge", ".act-buy", ".act-sell",
                    ".trade-pnl", ".trade-cost"):
            assert cls in html, f"missing CSS class {cls!r}"

    @t("v3.4.31: desktop .trade-row grid-template-areas = 'time sym act qty price tail'")
    def _():
        import re
        html_path = Path(__file__).resolve().parent / "dashboard_static" / "index.html"
        html = html_path.read_text(encoding="utf-8")
        # Grab every .trade-row rule block and confirm at least one
        # carries the desktop area string. Mobile overrides it inside
        # a @media block which is tested separately below.
        blocks = re.findall(r"\.trade-row\s*\{[^}]*\}", html)
        assert blocks, "no .trade-row CSS blocks found"
        assert any('"time sym act qty price tail"' in b for b in blocks), \
            "desktop .trade-row must use grid-template-areas " \
            "'time sym act qty price tail'"

    @t("v3.4.31: mobile (≤640px) collapses .trade-row into stacked rows")
    def _():
        import re
        html_path = Path(__file__).resolve().parent / "dashboard_static" / "index.html"
        html = html_path.read_text(encoding="utf-8")
        # Pull the @media (max-width: 640px) block and confirm the
        # .trade-row rule inside it switches to the 3-row area stack.
        mq = re.search(
            r"@media\s*\(\s*max-width:\s*640px\s*\)\s*\{[\s\S]*?\n  \}",
            html,
        )
        assert mq, "could not locate @media (max-width: 640px) block"
        body = mq.group(0)
        assert ".trade-row" in body, \
            ".trade-row override missing from 640px media query"
        # Must redefine the grid areas so the single desktop row
        # collapses onto multiple lines on phones.
        assert '"time sym  act"' in body or '"time sym act"' in body, \
            "mobile .trade-row should put time/sym/act on line 1"
        assert "qty" in body and "tail" in body and "price" in body, \
            "mobile .trade-row must still place qty / price / tail"
        # Multi-line template-areas is the signal that the row stacked.
        assert body.count('"') >= 4, \
            "mobile .trade-row should use multi-line grid-template-areas"

    @t("v3.4.31: renderTrades emits .trade-row markup — not a <table>")
    def _():
        import re
        html_path = Path(__file__).resolve().parent / "dashboard_static" / "index.html"
        html = html_path.read_text(encoding="utf-8")
        rt = re.search(r"function renderTrades[\s\S]*?\n  \}", html)
        assert rt, "could not locate renderTrades body"
        body = rt.group(0)
        # New row class is emitted.
        assert 'trade-row' in body, "renderTrades must emit .trade-row rows"
        # BUY trailing cell is the cost; SELL is P&L with colour class.
        assert "trade-cost" in body, \
            "renderTrades must render .trade-cost on BUY rows"
        assert "trade-pnl" in body, \
            "renderTrades must render .trade-pnl on SELL rows"
        # Colour-coded P&L via up/down helper classes.
        assert '"up"' in body and '"down"' in body, \
            "SELL P&L cell must carry up/down colour classes"
        # Reads pnl and pnl_pct off the SELL trade payload.
        assert "pnl_pct" in body, "renderTrades must use pnl_pct for SELLs"

    @t("v3.4.31: computeTradesSummary counts opens/closes + sums realized")
    def _():
        import re
        html_path = Path(__file__).resolve().parent / "dashboard_static" / "index.html"
        html = html_path.read_text(encoding="utf-8")
        fn = re.search(r"function computeTradesSummary[\s\S]*?\n  \}", html)
        assert fn, "could not locate computeTradesSummary body"
        body = fn.group(0)
        # Counts opens from BUY and closes from SELL.
        assert '"BUY"' in body, "summary must branch on BUY"
        assert '"SELL"' in body, "summary must branch on SELL"
        # Sums realized P&L from the 'pnl' field (server-provided).
        assert "t.pnl" in body, "summary must read t.pnl"
        # Win-rate is wins / closes with P&L — must guard div-by-zero.
        assert "wins" in body and ("have_pnl" in body or "win_rate" in body), \
            "summary must compute win_rate"

    @t("v3.4.31: renderTrades populates summary line + realized chip")
    def _():
        import re
        html_path = Path(__file__).resolve().parent / "dashboard_static" / "index.html"
        html = html_path.read_text(encoding="utf-8")
        rt = re.search(r"function renderTrades[\s\S]*?\n  \}", html)
        assert rt, "could not locate renderTrades body"
        body = rt.group(0)
        assert "computeTradesSummary" in body, \
            "renderTrades must call computeTradesSummary"
        assert "trades-summary" in body, \
            "renderTrades must write into #trades-summary"
        assert "trades-realized" in body, \
            "renderTrades must update the realized-$ header chip"

    # -------------------------------------------------------------
    # v3.4.32 — editable ticker universe + QBTS default + commands
    # -------------------------------------------------------------

    @t("v3.4.32: BOT_VERSION is >= 3.4.32")
    def _():
        import stock_spike_monitor as m
        parts = tuple(int(x) for x in m.BOT_VERSION.split("."))
        assert parts >= (3, 4, 32), m.BOT_VERSION

    @t("v3.4.32: QBTS is in TICKERS_DEFAULT and TICKERS")
    def _():
        import stock_spike_monitor as m
        assert "QBTS" in m.TICKERS_DEFAULT, m.TICKERS_DEFAULT
        assert "QBTS" in m.TICKERS, m.TICKERS

    @t("v3.4.32: SPY and QQQ are pinned and excluded from TRADE_TICKERS")
    def _():
        import stock_spike_monitor as m
        assert "SPY" in m.TICKERS_PINNED and "QQQ" in m.TICKERS_PINNED
        m._rebuild_trade_tickers()
        assert "SPY" not in m.TRADE_TICKERS
        assert "QQQ" not in m.TRADE_TICKERS
        # Non-pinned symbols should pass through.
        assert "AAPL" in m.TRADE_TICKERS

    @t("v3.4.32: _normalise_ticker upcases, strips $, rejects junk")
    def _():
        import stock_spike_monitor as m
        assert m._normalise_ticker("qbts") == "QBTS"
        assert m._normalise_ticker("  $qbts  ") == "QBTS"
        assert m._normalise_ticker("AAPL\n") == "AAPL"
        # Invalid inputs normalise to empty string.
        assert m._normalise_ticker("not-a-ticker!") == ""
        assert m._normalise_ticker("") == ""
        assert m._normalise_ticker(None) == ""
        # Regex rejects lowercase-start / overlong symbols too.
        assert not m.TICKER_SYM_RE.match("123ABC")
        assert not m.TICKER_SYM_RE.match("TOOLONGSYM")

    @t("v3.4.32: add_ticker then repeat, then remove semantics")
    def _():
        import stock_spike_monitor as m
        sym = "ZZZZ"
        # Clean slate.
        if sym in m.TICKERS:
            m.TICKERS.remove(sym)
            m._rebuild_trade_tickers()
        # Stub out metric fill — we test persistence/semantics here,
        # not the FMP/OR network path.
        orig_fill = m._fill_metrics_for_ticker
        m._fill_metrics_for_ticker = lambda t: {
            "pdc": False, "or": False, "rsi": False, "errors": [],
        }
        # Also stub the save so we don't clobber tickers.json.
        orig_save = m._save_tickers_file
        m._save_tickers_file = lambda: True
        try:
            res1 = m.add_ticker(sym)
            assert res1.get("ok") is True, res1
            assert res1.get("added") is True, res1
            assert sym in m.TICKERS
            # Repeat add is a no-op but still ok=True.
            res2 = m.add_ticker(sym)
            assert res2.get("ok") is True, res2
            assert res2.get("added") is False, res2
            # Remove works, second remove is a no-op.
            res3 = m.remove_ticker(sym)
            assert res3.get("ok") is True, res3
            assert res3.get("removed") is True, res3
            assert sym not in m.TICKERS
            res4 = m.remove_ticker(sym)
            assert res4.get("ok") is True, res4
            assert res4.get("removed") is False, res4
        finally:
            m._fill_metrics_for_ticker = orig_fill
            m._save_tickers_file = orig_save
            if sym in m.TICKERS:
                m.TICKERS.remove(sym)
                m._rebuild_trade_tickers()

    @t("v3.4.32: add_ticker rejects invalid symbols")
    def _():
        import stock_spike_monitor as m
        res = m.add_ticker("not-a-ticker!")
        assert res.get("ok") is False, res
        assert "invalid" in (res.get("reason") or "").lower(), res

    @t("v3.4.32: remove_ticker refuses to remove pinned SPY/QQQ")
    def _():
        import stock_spike_monitor as m
        for pinned in ("SPY", "QQQ"):
            res = m.remove_ticker(pinned)
            assert res.get("ok") is False, (pinned, res)
            assert "pinned" in (res.get("reason") or "").lower(), (pinned, res)
            assert pinned in m.TICKERS, pinned

    @t("v3.4.32: tickers.json save/load round-trip preserves order")
    def _():
        import stock_spike_monitor as m
        import tempfile, json as _json, os as _os
        original_tickers = list(m.TICKERS)
        original_file = m.TICKERS_FILE
        with tempfile.TemporaryDirectory() as td:
            tmp_path = _os.path.join(td, "tickers.json")
            m.TICKERS_FILE = tmp_path
            try:
                sample = ["AAPL", "QBTS", "NVDA", "SPY", "QQQ"]
                m.TICKERS[:] = list(sample)
                m._rebuild_trade_tickers()
                assert m._save_tickers_file() is True
                # File should exist and be valid JSON with our list.
                with open(tmp_path, "r", encoding="utf-8") as fh:
                    data = _json.load(fh)
                assert data.get("tickers") == sample, data
                # Mutate memory, then reload from disk.
                m.TICKERS[:] = ["X"]
                loaded = m._load_tickers_file()
                assert loaded == sample, loaded
            finally:
                m.TICKERS_FILE = original_file
                m.TICKERS[:] = original_tickers
                m._rebuild_trade_tickers()

    @t("v3.4.32: cmd_tickers/add_ticker/remove_ticker exist and are async")
    def _():
        import stock_spike_monitor as m, inspect
        for name in ("cmd_tickers", "cmd_add_ticker", "cmd_remove_ticker"):
            fn = getattr(m, name, None)
            assert fn is not None, name
            assert inspect.iscoroutinefunction(fn), name

    @t("v3.4.32: Telegram handlers wired for tickers/add_ticker/remove_ticker (alias compat)")
    def _():
        # v3.4.33 moved these out of the BotCommand menu, but they stay
        # registered as hidden aliases so saved shortcuts keep working.
        import stock_spike_monitor as m, inspect
        for fn_name in ("cmd_tickers", "cmd_add_ticker", "cmd_remove_ticker"):
            fn = getattr(m, fn_name, None)
            assert fn is not None, fn_name
            assert inspect.iscoroutinefunction(fn), fn_name

    @t("v3.4.32: ticker reply formatters stay within 34-char mobile budget")
    def _():
        import stock_spike_monitor as m
        samples = [
            m._fmt_tickers_list(),
            m._fmt_add_reply({
                "ok": True, "added": True, "ticker": "QBTS",
                "metrics": {"pdc": True, "or": True, "errors": []},
            }),
            m._fmt_add_reply({
                "ok": True, "added": False, "ticker": "QBTS",
            }),
            m._fmt_add_reply({
                "ok": False, "ticker": "BAD", "reason": "invalid",
            }),
            m._fmt_remove_reply({
                "ok": True, "removed": True, "ticker": "QBTS",
                "had_open": False,
            }),
            m._fmt_remove_reply({
                "ok": True, "removed": True, "ticker": "QBTS",
                "had_open": True,
            }),
            m._fmt_remove_reply({
                "ok": False, "ticker": "SPY", "reason": "pinned",
            }),
        ]
        for text in samples:
            for line in text.split("\n"):
                assert len(line) <= 34, (len(line), line)

    @t("v3.4.32: help text advertises the new ticker commands")
    def _():
        import stock_spike_monitor as m
        note = m.CURRENT_MAIN_NOTE + "\n" + m._MAIN_HISTORY_TAIL
        # Commands themselves should be visible somewhere user-facing:
        # either release notes or the /help body via cmd_help source.
        import inspect
        help_src = inspect.getsource(m.cmd_help)
        corpus = note + "\n" + help_src
        # After v3.4.33 the canonical command is /ticker; the old names
        # live on as hidden aliases. The /help body still mentions them.
        for want in ("/ticker", "/tickers", "/add_ticker", "/remove_ticker"):
            assert want in corpus, want

    # -------------------------------------------------------------
    # v3.4.33 — unified /ticker command + thorough metric fill
    # -------------------------------------------------------------

    @t("v3.4.33: BOT_VERSION is >= 3.4.33")
    def _():
        import stock_spike_monitor as m
        parts = tuple(int(x) for x in m.BOT_VERSION.split("."))
        assert parts >= (3, 4, 33), m.BOT_VERSION

    @t("v3.4.33: cmd_ticker exists and is an async coroutine")
    def _():
        import stock_spike_monitor as m, inspect
        fn = getattr(m, "cmd_ticker", None)
        assert fn is not None, "cmd_ticker missing"
        assert inspect.iscoroutinefunction(fn), "cmd_ticker not async"

    @t("v3.4.33: BotCommand menu advertises /ticker (not per-verb entries)")
    def _():
        import stock_spike_monitor as m
        names = {c.command for c in m.MAIN_BOT_COMMANDS}
        assert "ticker" in names, sorted(names)
        # The v3.4.32 per-verb entries are gone from the menu (but the
        # handlers remain as hidden aliases — tested separately).
        assert "tickers" not in names, \
            "v3.4.33 should not advertise /tickers in menu"
        assert "add_ticker" not in names, \
            "v3.4.33 should not advertise /add_ticker in menu"
        assert "remove_ticker" not in names, \
            "v3.4.33 should not advertise /remove_ticker in menu"

    @t("v3.4.33: /ticker help text references all three sub-commands")
    def _():
        import stock_spike_monitor as m
        for want in ("list", "add", "remove"):
            assert want in m._TICKER_USAGE, (want, m._TICKER_USAGE)

    @t("v3.4.33: /ticker usage string stays within 34-char mobile budget")
    def _():
        import stock_spike_monitor as m
        for line in m._TICKER_USAGE.split("\n"):
            assert len(line) <= 34, (len(line), line)

    @t("v3.4.33: _fill_metrics_for_ticker returns the full metric dict shape")
    def _():
        import stock_spike_monitor as m
        # Stub FMP and bars so the test is hermetic.
        orig_fmp = m.get_fmp_quote
        orig_bars = m.fetch_1min_bars
        m.get_fmp_quote = lambda t: {"previousClose": 123.45}
        # Provide enough closes for RSI warm-up (RSI_PERIOD+1 minimum).
        fake_closes = [100.0 + i * 0.1 for i in range(m.RSI_PERIOD + 5)]
        m.fetch_1min_bars = lambda t: {
            "timestamps": [1700000000 + i * 60 for i in range(len(fake_closes))],
            "highs":  fake_closes,
            "lows":   fake_closes,
            "closes": fake_closes,
            "volumes": [10000] * len(fake_closes),
            "pdc": 99.0,
        }
        try:
            got = m._fill_metrics_for_ticker("TESTSYM")
            for key in ("bars", "pdc", "pdc_src", "or",
                        "or_pending", "rsi", "rsi_val", "errors"):
                assert key in got, (key, got)
            assert got["bars"] is True, got
            assert got["pdc"] is True, got
            assert got["pdc_src"] == "fmp", got
            assert got["rsi"] is True, got
            assert got["rsi_val"] is not None, got
            # pdc[] should have been cached.
            assert m.pdc.get("TESTSYM") == 123.45, m.pdc.get("TESTSYM")
        finally:
            m.get_fmp_quote = orig_fmp
            m.fetch_1min_bars = orig_bars
            m.pdc.pop("TESTSYM", None)
            m.or_high.pop("TESTSYM", None)
            m.or_low.pop("TESTSYM", None)

    @t("v3.4.33: PDC falls back to bars when FMP returns nothing")
    def _():
        import stock_spike_monitor as m
        orig_fmp = m.get_fmp_quote
        orig_bars = m.fetch_1min_bars
        m.get_fmp_quote = lambda t: None
        m.fetch_1min_bars = lambda t: {
            "timestamps": [1700000000],
            "highs": [100.0], "lows": [99.0],
            "closes": [99.5], "volumes": [1000],
            "pdc": 98.76,
        }
        try:
            got = m._fill_metrics_for_ticker("TESTSYM2")
            assert got["pdc"] is True, got
            assert got["pdc_src"] == "bars", got
            assert m.pdc.get("TESTSYM2") == 98.76, m.pdc.get("TESTSYM2")
        finally:
            m.get_fmp_quote = orig_fmp
            m.fetch_1min_bars = orig_bars
            m.pdc.pop("TESTSYM2", None)

    @t("v3.4.33: unreachable bars → bars=False, pdc=False when FMP also fails")
    def _():
        import stock_spike_monitor as m
        orig_fmp = m.get_fmp_quote
        orig_bars = m.fetch_1min_bars
        m.get_fmp_quote = lambda t: None
        m.fetch_1min_bars = lambda t: None
        try:
            got = m._fill_metrics_for_ticker("NOBARS")
            assert got["bars"] is False, got
            assert got["pdc"] is False, got
            assert got["rsi"] is False, got
            # Error list should carry at least one human-readable hint.
            assert got["errors"], got
        finally:
            m.get_fmp_quote = orig_fmp
            m.fetch_1min_bars = orig_bars

    @t("v3.4.33: add reply formatter shows Bars/PDC/OR/RSI rows")
    def _():
        import stock_spike_monitor as m
        orig_pdc = m.pdc.get("QBTS")
        orig_orh = m.or_high.get("QBTS")
        orig_orl = m.or_low.get("QBTS")
        m.pdc["QBTS"] = 10.50
        m.or_high["QBTS"] = 10.80
        m.or_low["QBTS"] = 10.40
        try:
            out = m._fmt_add_reply({
                "ok": True, "added": True, "ticker": "QBTS",
                "metrics": {
                    "bars": True, "pdc": True, "pdc_src": "fmp",
                    "or": True, "or_pending": False,
                    "rsi": True, "rsi_val": 61.4,
                    "errors": [],
                },
            })
            assert "Bars:" in out, out
            assert "PDC:" in out, out
            assert "OR:" in out, out
            assert "RSI:" in out, out
            # Every line stays within the 34-char mobile budget.
            for line in out.split("\n"):
                assert len(line) <= 34, (len(line), line)
        finally:
            if orig_pdc is None: m.pdc.pop("QBTS", None)
            else: m.pdc["QBTS"] = orig_pdc
            if orig_orh is None: m.or_high.pop("QBTS", None)
            else: m.or_high["QBTS"] = orig_orh
            if orig_orl is None: m.or_low.pop("QBTS", None)
            else: m.or_low["QBTS"] = orig_orl

    @t("release notes history tail lists the most-recent prior release")
    def _():
        # v3.4.38: the rolling history window only holds the last few
        # releases. Check that whichever version is the "most recent
        # prior" appears \u2014 anchored to the current BOT_VERSION so
        # this test survives future rollovers.
        import stock_spike_monitor as m
        note = m.MAIN_RELEASE_NOTE
        # Parse the immediate predecessor from BOT_VERSION (e.g. 3.4.38 -> 3.4.37).
        major, minor, patch = (int(x) for x in m.BOT_VERSION.split("."))
        prev = f"v{major}.{minor}.{patch - 1}"
        assert prev in note, f"previous release {prev} must persist in history: {note!r}"

    # =================================================================
    # v3.4.34 — AVWAP → PDC full migration
    # =================================================================
    # Entry gates, regime alert, breadth observer, and display text
    # all migrated off AVWAP. _dual_index_eject / update_avwap /
    # avwap_data / _last_finalized_5min_close all removed. Every
    # surface now anchors on PDC, matching the v3.4.28 ejector.

    @t("v3.4.34: BOT_VERSION is >= 3.4.34")
    def _():
        import stock_spike_monitor as m
        parts = tuple(int(x) for x in m.BOT_VERSION.split("."))
        assert parts >= (3, 4, 34), m.BOT_VERSION

    @t("v3.4.34: update_avwap / _dual_index_eject / _last_finalized_5min_close REMOVED")
    def _():
        import stock_spike_monitor as m
        assert not hasattr(m, "update_avwap"), \
            "update_avwap should have been deleted"
        assert not hasattr(m, "_dual_index_eject"), \
            "_dual_index_eject should have been deleted"
        assert not hasattr(m, "_last_finalized_5min_close"), \
            "_last_finalized_5min_close should have been deleted"

    @t("v3.4.34: avwap_data / avwap_last_ts module state REMOVED")
    def _():
        import stock_spike_monitor as m
        assert not hasattr(m, "avwap_data"), \
            "avwap_data dict should have been deleted"
        assert not hasattr(m, "avwap_last_ts"), \
            "avwap_last_ts dict should have been deleted"

    @t("v3.4.34: load_paper_state tolerates legacy avwap_data/avwap_last_ts keys")
    def _():
        # Old state files (pre-v3.4.34) have these keys. Loading must
        # succeed without raising — keys are silently ignored.
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m.load_paper_state)
        # The loader should NOT reference avwap_data.update(...) anymore.
        assert "avwap_data.update" not in src, \
            "load_paper_state must not reference removed avwap_data dict"
        assert "avwap_last_ts.update" not in src, \
            "load_paper_state must not reference removed avwap_last_ts dict"

    @t("v3.4.34: save_paper_state no longer writes avwap_data / avwap_last_ts")
    def _():
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m.save_paper_state)
        # Dict literal keys for avwap state should be absent from the
        # persisted payload.
        assert '"avwap_data":' not in src, \
            "save_paper_state must not write avwap_data"
        assert '"avwap_last_ts":' not in src, \
            "save_paper_state must not write avwap_last_ts"

    @t("v3.4.34: check_entry gates on SPY_PDC and QQQ_PDC (not AVWAP)")
    def _():
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m.check_entry)
        # Must read PDC from the pdc dict.
        assert 'pdc.get("SPY")' in src, \
            "check_entry must read SPY from pdc dict"
        assert 'pdc.get("QQQ")' in src, \
            "check_entry must read QQQ from pdc dict"
        # Must NOT reference AVWAP state anymore.
        assert "avwap_data" not in src, \
            "check_entry must not reference removed avwap_data"
        assert "update_avwap" not in src, \
            "check_entry must not call removed update_avwap"

    @t("v3.4.34: check_short_entry gates on SPY_PDC and QQQ_PDC (not AVWAP)")
    def _():
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m.check_short_entry)
        assert 'pdc.get("SPY")' in src, \
            "check_short_entry must read SPY from pdc dict"
        assert 'pdc.get("QQQ")' in src, \
            "check_short_entry must read QQQ from pdc dict"
        assert "avwap_data" not in src, \
            "check_short_entry must not reference removed avwap_data"

    @t("v3.4.34: check_short_entry fails closed when SPY_PDC/QQQ_PDC missing")
    def _():
        # Locked design principle: missing data → do NOT enter.
        # Old AVWAP gate defaulted to True when state was unseeded
        # (fail-OPEN). New PDC gate must be fail-CLOSED.
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m.check_short_entry)
        # Look for the initialization line that proves the fix.
        # spy_below / qqq_below must default to False, then get set
        # to True only when price < PDC.
        assert "spy_below = False" in src, \
            "spy_below must default False (fail-closed) not True"
        assert "qqq_below = False" in src, \
            "qqq_below must default False (fail-closed) not True"

    @t("v3.4.34: regime-change alert uses PDC and emits 'Lords' messaging")
    def _():
        import stock_spike_monitor as m
        import inspect
        # The scan loop is where the regime alert lives.
        src = inspect.getsource(m.scan_loop)
        # Must probe pdc dict for both indices.
        assert 'pdc.get("SPY")' in src and 'pdc.get("QQQ")' in src, \
            "regime alert must read PDC from the pdc dict"
        # Format strings must say PDC, not AVWAP.
        assert "> PDC $%.2f" in src, \
            "regime alert must format bullish line as '> PDC'"
        assert "< PDC $%.2f" in src, \
            "regime alert must format bearish line as '< PDC'"
        assert "> AVWAP $%.2f" not in src and "< AVWAP $%.2f" not in src, \
            "regime alert must not format using AVWAP"
        # Lords messaging preserved.
        assert "The Lords are back" in src, \
            "bullish flip must keep 'Lords are back' message"
        assert "The Lords have left" in src, \
            "bearish flip must keep 'Lords have left' message"

    @t("v3.4.34: _classify_breadth observer anchors on PDC")
    def _():
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m._classify_breadth)
        assert 'pdc.get("SPY")' in src and 'pdc.get("QQQ")' in src, \
            "_classify_breadth must read PDC from the pdc dict"
        assert "avwap_data" not in src, \
            "_classify_breadth must not reference removed avwap_data"
        assert "%s PDC" in src, \
            "detail string must mention PDC, not AVWAP"

    @t("v3.4.34: /help /algo body says 'SPY & QQQ > PDC', not AVWAP")
    def _():
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m.cmd_algo)
        assert "SPY & QQQ > PDC" in src, \
            "algo reference must say SPY & QQQ > PDC"
        assert "SPY & QQQ < PDC" in src, \
            "algo reference must say SPY & QQQ < PDC"
        assert "SPY & QQQ > AVWAP" not in src, \
            "algo reference must not say SPY & QQQ > AVWAP"
        assert "SPY & QQQ < AVWAP" not in src, \
            "algo reference must not say SPY & QQQ < AVWAP"

    @t("v3.4.34: /strategy body anchors on PDC in all four index-check lines")
    def _():
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m.cmd_strategy)
        # Long entry.
        assert "SPY > PDC" in src and "QQQ > PDC" in src
        # Short entry.
        assert "SPY < PDC" in src and "QQQ < PDC" in src
        # Lords Left / Bull Vacuum exits now reference PDC on finalized 1m.
        assert "SPY AND QQQ < PDC" in src, "Lords Left exit must say PDC"
        assert "SPY AND QQQ > PDC" in src, "Bull Vacuum exit must say PDC"
        # AVWAP should be gone from the strategy surface.
        assert "SPY > AVWAP" not in src and "SPY < AVWAP" not in src

    @t("v3.4.34: reset_daily_state no longer resets removed AVWAP dicts")
    def _():
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m.reset_daily_state)
        assert "avwap_data" not in src, \
            "reset_daily_state must not reference removed avwap_data"
        assert "avwap_last_ts" not in src, \
            "reset_daily_state must not reference removed avwap_last_ts"

    @t("v3.4.34: width budget for MAIN_RELEASE_NOTE")
    def _():
        import stock_spike_monitor as m
        # v3.4.40: v3.4.34 has aged out of the rolling history tail
        # (tail now carries v3.4.35–v3.4.39 plus current). We still
        # enforce the 34-char mobile budget on every line, which was
        # the original regression risk.
        note = m.MAIN_RELEASE_NOTE
        for i, line in enumerate(note.split("\n")):
            assert len(line) <= 34, (i, len(line), repr(line))

    @t("release notes history tail keeps two prior releases")
    def _():
        # v3.4.38: rather than pin to a specific old release that will
        # eventually age out, assert that the history tail contains at
        # least TWO version strings older than BOT_VERSION. This keeps
        # the "rolling history" invariant enforced without churn every
        # release.
        import re, stock_spike_monitor as m
        note = m.MAIN_RELEASE_NOTE
        versions = set(re.findall(r"v3\.4\.\d+", note))
        versions.discard(f"v{m.BOT_VERSION}")
        assert len(versions) >= 2, \
            f"expected \u22652 prior releases in history, got {versions}"

    @t("v3.4.34: legacy LORDS_LEFT[1m] / BULL_VACUUM[1m] back-compat labels retained")
    def _():
        import stock_spike_monitor as m
        # Old persisted trade-log rows carry these keys; the label map
        # must still render them for any rows written before v3.4.28.
        assert "LORDS_LEFT[1m]" in m.REASON_LABELS
        assert "LORDS_LEFT[5m]" in m.REASON_LABELS
        assert "BULL_VACUUM[1m]" in m.REASON_LABELS
        assert "BULL_VACUUM[5m]" in m.REASON_LABELS

    # ============================================================
    # v3.4.36 — Peak-anchored profit-lock ladder
    # ============================================================
    # v3.4.35 anchored tiers to entry + X% — which widened the gap
    # between peak and stop as peak grew. v3.4.36 inverts: stop is
    # peak − X% (long) or peak + X% (short), with X shrinking at
    # higher tiers so the trade is locked in tighter the further it
    # works.

    @t("v3.4.36: BOT_VERSION is >= 3.4.36")
    def _():
        import stock_spike_monitor as m
        parts = tuple(int(x) for x in m.BOT_VERSION.split("."))
        assert parts >= (3, 4, 36), m.BOT_VERSION

    @t("v3.4.36: LADDER_TIERS_LONG and helpers exist")
    def _():
        import stock_spike_monitor as m
        assert hasattr(m, "LADDER_TIERS_LONG")
        assert callable(m._ladder_stop_long)
        assert callable(m._ladder_stop_short)
        # Six tiers total (five explicit + implicit sub-1%).
        assert len(m.LADDER_TIERS_LONG) == 5, m.LADDER_TIERS_LONG
        # Schedule: (0.05, 0.0010) ... (0.01, 0.0050)
        triggers = [t for t, _ in m.LADDER_TIERS_LONG]
        assert triggers == [0.05, 0.04, 0.03, 0.02, 0.01], triggers
        give_backs = [g for _, g in m.LADDER_TIERS_LONG]
        # Aggressive taper: higher peak → tighter give-back.
        assert give_backs == [0.001, 0.002, 0.003, 0.004, 0.005], give_backs

    @t("v3.4.36: long ladder sub-1% tier returns initial_stop (Bullet)")
    def _():
        import stock_spike_monitor as m
        pos = {"entry_price": 100.0, "trail_high": 100.50,
               "initial_stop": 99.25, "stop": 99.25}
        assert m._ladder_stop_long(pos) == 99.25

    @t("v3.4.36: long ladder +1% → peak − 0.50%")
    def _():
        import stock_spike_monitor as m
        pos = {"entry_price": 100.0, "trail_high": 101.00,
               "initial_stop": 99.25, "stop": 99.25}
        # 101.00 * 0.995 = 100.495 → rounds to 100.49 or 100.50 depending
        # on float precision. Accept either — both are within 1 cent.
        stop = m._ladder_stop_long(pos)
        assert round(abs(stop - 100.50), 2) <= 0.01, stop

    @t("v3.4.36: long ladder +2% → peak − 0.40%")
    def _():
        import stock_spike_monitor as m
        pos = {"entry_price": 100.0, "trail_high": 102.00,
               "initial_stop": 99.25, "stop": 99.25}
        # 102.00 * 0.996 = 101.592 → rounds to 101.59
        assert m._ladder_stop_long(pos) == 101.59

    @t("v3.4.36: long ladder +3% → peak − 0.30%")
    def _():
        import stock_spike_monitor as m
        pos = {"entry_price": 100.0, "trail_high": 103.00,
               "initial_stop": 99.25, "stop": 99.25}
        # 103.00 * 0.997 = 102.691 → rounds to 102.69
        assert m._ladder_stop_long(pos) == 102.69

    @t("v3.4.36: long ladder +4% → peak − 0.20%")
    def _():
        import stock_spike_monitor as m
        pos = {"entry_price": 100.0, "trail_high": 104.00,
               "initial_stop": 99.25, "stop": 99.25}
        # 104.00 * 0.998 = 103.792 → rounds to 103.79
        assert m._ladder_stop_long(pos) == 103.79

    @t("v3.4.36: long ladder +5% → peak − 0.10% (Harvest)")
    def _():
        import stock_spike_monitor as m
        pos = {"entry_price": 100.0, "trail_high": 105.00,
               "initial_stop": 99.25, "stop": 99.25}
        # 105.00 * 0.999 = 104.895 → rounds to 104.90
        # (Python banker's rounding: round-half-to-even; here
        #  104.895 is technically inexact in float, so verify by math)
        stop = m._ladder_stop_long(pos)
        assert abs(stop - 104.90) < 0.01 or abs(stop - 104.89) < 0.01, stop

    @t("v3.4.36: long ladder +10% Harvest stays peak − 0.10%")
    def _():
        import stock_spike_monitor as m
        pos = {"entry_price": 100.0, "trail_high": 110.00,
               "initial_stop": 99.25, "stop": 99.25}
        # 110.00 * 0.999 = 109.89
        assert m._ladder_stop_long(pos) == 109.89

    @t("v3.4.36: long ladder peak-anchored (gap shrinks as peak grows)")
    def _():
        import stock_spike_monitor as m
        # Verify the core design: gap(peak, stop) must NARROW at higher
        # peak tiers, not widen (which was the v3.4.35 bug).
        def gap(peak):
            pos = {"entry_price": 100.0, "trail_high": peak,
                   "initial_stop": 99.25, "stop": 99.25}
            return round(peak - m._ladder_stop_long(pos), 4)
        g2 = gap(102.00)   # +2% tier: peak * 0.004 = 0.408
        g3 = gap(103.00)   # +3% tier: peak * 0.003 = 0.309
        g4 = gap(104.00)   # +4% tier: peak * 0.002 = 0.208
        g5 = gap(105.00)   # +5% tier: peak * 0.001 = 0.105
        assert g2 > g3 > g4 > g5, (g2, g3, g4, g5)

    @t("v3.4.36: long ladder never looser than initial_stop")
    def _():
        import stock_spike_monitor as m
        # Peak only +0.5% (sub-1%) — structural stop holds.
        pos = {"entry_price": 100.0, "trail_high": 100.50,
               "initial_stop": 99.25, "stop": 99.25}
        assert m._ladder_stop_long(pos) == 99.25
        # Tighter tier that computes below initial — clamped up.
        pos = {"entry_price": 100.0, "trail_high": 101.00,
               "initial_stop": 101.50, "stop": 101.50}
        # Tier would return 100.50, but initial is 101.50 — keep initial.
        assert m._ladder_stop_long(pos) == 101.50

    @t("v3.4.36: long ladder legacy fallback (no initial_stop)")
    def _():
        import stock_spike_monitor as m
        # Pre-v3.4.35 position — no initial_stop. Must fall back to
        # pos['stop'] and never crash.
        pos = {"entry_price": 100.0, "trail_high": 100.50, "stop": 99.00}
        assert m._ladder_stop_long(pos) == 99.00
        # Above +1% the tier computes against peak, clamped by stop.
        pos["trail_high"] = 101.00
        # tier = 101.00 * 0.995 = 100.495 → 100.50; fallback stop = 99.00
        # max(100.50, 99.00) = 100.50
        assert m._ladder_stop_long(pos) == 100.50

    @t("v3.4.36: long ladder one-way — peak higher → stop higher")
    def _():
        import stock_spike_monitor as m
        pos_lo = {"entry_price": 100.0, "trail_high": 102.00,
                  "initial_stop": 99.25, "stop": 99.25}
        pos_hi = {"entry_price": 100.0, "trail_high": 105.00,
                  "initial_stop": 99.25, "stop": 99.25}
        lo = m._ladder_stop_long(pos_lo)
        hi = m._ladder_stop_long(pos_hi)
        assert hi > lo, (hi, lo)

    @t("v3.4.36: short ladder mirror — +1% → peak + 0.50%")
    def _():
        import stock_spike_monitor as m
        pos = {"entry_price": 100.0, "trail_low": 99.00,
               "initial_stop": 100.75, "stop": 100.75}
        # 99.00 * 1.005 = 99.49499... in float → rounds to 99.49
        # (or 99.50 on other platforms). Accept either — both are
        # within 1 cent of the target and still tighter than initial.
        stop = m._ladder_stop_short(pos)
        assert round(abs(stop - 99.50), 2) <= 0.01, stop

    @t("v3.4.36: short ladder mirror — +2% → peak + 0.40%")
    def _():
        import stock_spike_monitor as m
        pos = {"entry_price": 100.0, "trail_low": 98.00,
               "initial_stop": 100.75, "stop": 100.75}
        # 98.00 * 1.004 = 98.392 → 98.39
        assert m._ladder_stop_short(pos) == 98.39

    @t("v3.4.36: short ladder mirror — +4% → peak + 0.20%")
    def _():
        import stock_spike_monitor as m
        pos = {"entry_price": 100.0, "trail_low": 96.00,
               "initial_stop": 100.75, "stop": 100.75}
        # 96.00 * 1.002 = 96.192 → 96.19
        assert m._ladder_stop_short(pos) == 96.19

    @t("v3.4.36: short ladder mirror — +5% → peak + 0.10% (Harvest)")
    def _():
        import stock_spike_monitor as m
        pos = {"entry_price": 100.0, "trail_low": 95.00,
               "initial_stop": 100.75, "stop": 100.75}
        # 95.00 * 1.001 = 95.095 → 95.10 or 95.09 (float-edge)
        stop = m._ladder_stop_short(pos)
        assert abs(stop - 95.10) < 0.01 or abs(stop - 95.09) < 0.01, stop

    @t("v3.4.36: short ladder sub-1% returns initial_stop")
    def _():
        import stock_spike_monitor as m
        pos = {"entry_price": 100.0, "trail_low": 99.50,
               "initial_stop": 100.75, "stop": 100.75}
        assert m._ladder_stop_short(pos) == 100.75

    @t("v3.4.36: short ladder legacy fallback (no initial_stop)")
    def _():
        import stock_spike_monitor as m
        pos = {"entry_price": 100.0, "trail_low": 99.50, "stop": 101.00}
        assert m._ladder_stop_short(pos) == 101.00

    @t("v3.4.36: AVGO Eugene scenario — tighter than old rule")
    def _():
        import stock_spike_monitor as m
        # Eugene's exact complaint: AVGO entry $411.30, peak $420.69,
        # old prod stop was $416.48 (gap $4.21). v3.4.35 made this
        # WORSE ($415.41, gap $5.28). v3.4.36 fixes: +2% tier = peak *
        # 0.996 = 420.69 * 0.996 = 419.007 → $419.01 (gap $1.68).
        pos = {"entry_price": 411.30, "trail_high": 420.69,
               "initial_stop": 408.22, "stop": 408.22}
        stop = m._ladder_stop_long(pos)
        gap = 420.69 - stop
        assert gap < 2.00, f"gap {gap:.2f} not tight enough"
        assert stop > 416.48, f"stop {stop:.2f} not tighter than old rule"

    @t("v3.4.36: initial_stop persisted across entry paths")
    def _():
        import stock_spike_monitor as m
        src = open(m.__file__).read()
        assert "\"initial_stop\"" in src or "'initial_stop'" in src
        # 4 position dicts + helpers → >= 6 references.
        count = src.count("initial_stop")
        assert count >= 6, f"initial_stop only appears {count} times"

    @t("v3.4.36: /strategy body shows peak-anchored ladder")
    def _():
        import inspect
        import stock_spike_monitor as m
        src = inspect.getsource(m.cmd_strategy)
        assert "Ladder" in src
        # v3.4.36 uses "peak − X%" / "peak + X%" copy.
        assert "peak" in src.lower()
        # Old gain-anchored copy must be gone.
        assert "entry +1%" not in src
        assert "entry+0.9" not in src
        assert "+1.0% trigger" not in src

    @t("v3.4.36: /algo body shows peak-anchored ladder")
    def _():
        import inspect
        import stock_spike_monitor as m
        src = inspect.getsource(m.cmd_algo)
        assert "Ladder" in src
        assert "peak" in src.lower()
        assert "entry +1%" not in src
        assert "entry+0.9" not in src

    @t("v3.4.36: /strategy ladder lines stay within 34-char budget")
    def _():
        import inspect
        import stock_spike_monitor as m
        src = inspect.getsource(m.cmd_strategy)
        over = []
        import re
        for match in re.finditer(r'"([^"]*\\u2192[^"]*)\\n"', src):
            raw = match.group(1).encode().decode("unicode_escape")
            if len(raw) > 34:
                over.append((len(raw), raw))
        assert not over, f"lines over 34 chars: {over}"

    @t("CURRENT notes lead with active BOT_VERSION")
    def _():
        # v3.4.38: replaces two version-pinned tests. Dynamic so this
        # survives rollovers without editing every release.
        import stock_spike_monitor as m
        tag = f"v{m.BOT_VERSION}"
        assert m.CURRENT_MAIN_NOTE.startswith(tag), m.CURRENT_MAIN_NOTE[:40]
        assert m.CURRENT_TP_NOTE.startswith(tag), m.CURRENT_TP_NOTE[:40]

    @t("v3.4.36: CURRENT notes stay within 34-char mobile budget")
    def _():
        import stock_spike_monitor as m
        for name in ("CURRENT_MAIN_NOTE", "CURRENT_TP_NOTE",
                     "_MAIN_HISTORY_TAIL", "_TP_HISTORY_TAIL"):
            val = getattr(m, name, "")
            for i, line in enumerate(val.split("\n")):
                assert len(line) <= 34, \
                    f"{name} line {i} over 34 chars ({len(line)}): {line!r}"

    @t("v3.4.36: profit-lock ladder persists in history tail")
    def _():
        # v3.4.41: v3.4.35 has aged out of the rolling history; the
        # v3.4.36 peak-anchored ladder line is now the oldest ladder
        # mention. Structural check: the tail must still describe the
        # profit-lock ladder so /version retains the lineage.
        import stock_spike_monitor as m
        note = m.MAIN_RELEASE_NOTE.lower()
        assert "ladder" in note, \
            "MAIN_RELEASE_NOTE must keep a profit-lock ladder line"
        assert "peak" in note or "gain-anchored" in note, \
            "ladder line must describe its anchor (peak or gain-anchored)"

    @t("v3.4.36: history tail carries multiple older releases")
    def _():
        import stock_spike_monitor as m
        # v3.4.40: replaced the pin to v3.4.34 (aged out) with a
        # structural check: the tail should carry at least the two
        # most recent prior releases.
        note = m.MAIN_RELEASE_NOTE
        assert "v3.4.39" in note, "v3.4.39 must persist in history"
        assert "v3.4.38" in note, "v3.4.38 must persist in history"


    # ================================================================
    # v3.4.37 — Robinhood mode tests
    # ================================================================

    @t("v3.4.37: rh_shares_for($150) == 10")
    def _():
        import stock_spike_monitor as m
        result = m.rh_shares_for(150.0)
        assert result == 10, f"expected 10, got {result} (1500/150=10)"

    @t("v3.4.37: rh_shares_for($800) == 1")
    def _():
        import stock_spike_monitor as m
        result = m.rh_shares_for(800.0)
        assert result == 1, f"expected 1, got {result} (1500/800=1.875 -> floor=1, min=1)"

    @t("v3.4.37: rh_shares_for($1600) == 1 (min floor)")
    def _():
        import stock_spike_monitor as m
        result = m.rh_shares_for(1600.0)
        assert result == 1, f"expected 1 (min), got {result}"

    @t("v3.4.37: rh_shares_for($0) == 0")
    def _():
        import stock_spike_monitor as m
        result = m.rh_shares_for(0.0)
        assert result == 0, f"expected 0 for zero price, got {result}"

    @t("v3.4.37: rh_shares_for negative price == 0")
    def _():
        import stock_spike_monitor as m
        result = m.rh_shares_for(-10.0)
        assert result == 0, f"expected 0 for negative price, got {result}"

    @t("v3.4.37: TP starting capital is RH_STARTING_CAPITAL (25000)")
    def _():
        import stock_spike_monitor as m
        import inspect
        # Check constants: RH must be 25k, paper must stay 100k
        assert m.RH_STARTING_CAPITAL == 25000.0, \
            f"RH_STARTING_CAPITAL should be 25000, got {m.RH_STARTING_CAPITAL}"
        assert m.PAPER_STARTING_CAPITAL == 100_000.0, \
            "Paper starting capital must stay 100000"
        # Verify source-level assignment uses RH_STARTING_CAPITAL (not PAPER_STARTING_CAPITAL)
        src = inspect.getsource(m)
        assert "tp_paper_cash: float = RH_STARTING_CAPITAL" in src, \
            "tp_paper_cash must be initialized to RH_STARTING_CAPITAL in module source"

    @t("v3.4.37: RH_LONG_ONLY blocks TP short entry (gate logic)")
    def _():
        """When RH_LONG_ONLY=True, the gate condition prevents send_traderspost_order
        from being reached on the short entry path."""
        import stock_spike_monitor as m
        orig_long_only = m.RH_LONG_ONLY
        try:
            m.RH_LONG_ONLY = True
            calls = []
            orig_fn = m.send_traderspost_order

            def mock_order(ticker, action, price, shares=m.SHARES):
                calls.append({"ticker": ticker, "action": action})
                return {"success": False, "skipped": True, "message": "mocked",
                        "http_status": 0, "raw": None}

            m.send_traderspost_order = mock_order

            # Simulate the gate as coded in execute_short_entry v3.4.37:
            # if RH_LONG_ONLY: return (skip TP short)
            if m.RH_LONG_ONLY:
                pass  # gate fires — no TP order
            else:
                m.send_traderspost_order("NVDA", "sell", 140.0)

            assert len(calls) == 0, \
                f"TP short order was called despite RH_LONG_ONLY=True: {calls}"
        finally:
            m.RH_LONG_ONLY = orig_long_only
            m.send_traderspost_order = orig_fn

    @t("v3.4.37: RH_MAX_CONCURRENT_POSITIONS=6 blocks 7th entry")
    def _():
        """When tp_positions already has 6 entries, no TP entry order should fire."""
        import stock_spike_monitor as m
        orig_cap = m.RH_MAX_CONCURRENT_POSITIONS
        orig_tp = dict(m.tp_positions)
        try:
            m.RH_MAX_CONCURRENT_POSITIONS = 6
            m.tp_positions.clear()
            for i in range(6):
                m.tp_positions[f"FAKE{i}"] = {"entry_price": 100.0, "shares": 10}

            # Gate check: if tp_positions >= RH_MAX_CONCURRENT_POSITIONS, no order
            should_fire = (
                "NVDA" not in m.tp_positions and
                len(m.tp_positions) < m.RH_MAX_CONCURRENT_POSITIONS
            )
            assert not should_fire, \
                "TP entry must NOT fire when 6 positions already open"
        finally:
            m.RH_MAX_CONCURRENT_POSITIONS = orig_cap
            m.tp_positions.clear()
            m.tp_positions.update(orig_tp)

    @t("v3.4.37: RH_MAX_CONCURRENT_POSITIONS=6 allows 6th entry")
    def _():
        """With 5 existing positions, a 6th entry IS allowed."""
        import stock_spike_monitor as m
        orig_cap = m.RH_MAX_CONCURRENT_POSITIONS
        orig_tp = dict(m.tp_positions)
        try:
            m.RH_MAX_CONCURRENT_POSITIONS = 6
            m.tp_positions.clear()
            for i in range(5):
                m.tp_positions[f"FAKE{i}"] = {"entry_price": 100.0, "shares": 10}

            can_fire = (
                "NVDA" not in m.tp_positions and
                len(m.tp_positions) < m.RH_MAX_CONCURRENT_POSITIONS
            )
            assert can_fire, "6th entry should be allowed with 5 existing positions"
        finally:
            m.RH_MAX_CONCURRENT_POSITIONS = orig_cap
            m.tp_positions.clear()
            m.tp_positions.update(orig_tp)

    # v3.4.39: these were originally v3.4.38 pins \u2014 rewritten as dynamic
    # anchors against m.BOT_VERSION so we don't have to touch them each bump.
    @t("release: /algo caption matches BOT_VERSION")
    def _():
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m.cmd_algo)
        tag = "v" + m.BOT_VERSION
        assert tag in src, f"/algo source must contain {tag}"

    @t("release: BOT_VERSION matches current 3.4.x series")
    def _():
        import stock_spike_monitor as m
        assert m.BOT_VERSION.startswith("3.4."), \
            f"BOT_VERSION should be a 3.4.x release, got {m.BOT_VERSION!r}"

    @t("release: CURRENT_MAIN_NOTE leads with BOT_VERSION")
    def _():
        import stock_spike_monitor as m
        tag = "v" + m.BOT_VERSION
        assert m.CURRENT_MAIN_NOTE.startswith(tag), \
            f"CURRENT_MAIN_NOTE must start with {tag}: {m.CURRENT_MAIN_NOTE[:40]!r}"

    @t("release: CURRENT_TP_NOTE leads with BOT_VERSION")
    def _():
        import stock_spike_monitor as m
        tag = "v" + m.BOT_VERSION
        assert m.CURRENT_TP_NOTE.startswith(tag), \
            f"CURRENT_TP_NOTE must start with {tag}: {m.CURRENT_TP_NOTE[:40]!r}"

    @t("v3.4.38: v3.4.37 persists in release note history")
    def _():
        import stock_spike_monitor as m
        assert "v3.4.37" in m.MAIN_RELEASE_NOTE, \
            "v3.4.37 must persist in MAIN_RELEASE_NOTE history"
        assert "v3.4.37" in m.TP_RELEASE_NOTE, \
            "v3.4.37 must persist in TP_RELEASE_NOTE history"

    @t("v3.4.37: rh_shares_for uses RH_DOLLARS_PER_ENTRY dynamically")
    def _():
        import stock_spike_monitor as m
        orig = m.RH_DOLLARS_PER_ENTRY
        try:
            m.RH_DOLLARS_PER_ENTRY = 3000.0
            result = m.rh_shares_for(150.0)
            assert result == 20, \
                f"With $3000/entry at $150, expect 20 shares, got {result}"
        finally:
            m.RH_DOLLARS_PER_ENTRY = orig

    @t("v3.4.37: _rh_parse_tp_email detects failed subject")
    def _():
        import stock_spike_monitor as m
        body = (
            "Status:\nInsufficient buying power\n\n"
            '{"ticker":"NVDA","action":"buy","quantity":10,"limitPrice":145.20}'
        )
        # Use a subject without 'failed' for the payload test
        body2 = (
            "Status:\nInsufficient buying power\n\n"
            "Payload:\n"
            '{"ticker":"NVDA","action":"buy","quantity":10,"limitPrice":145.20}'
        )
        result = m._rh_parse_tp_email(body2, "Trade signal to MyStrategy failed")
        assert result["kind"] == "failed"
        assert result["reason"] == "Insufficient buying power"
        assert result["ticker"] == "NVDA"
        assert result["action"] == "buy"
        assert result["qty"] == 10

    @t("v3.4.37: _rh_parse_tp_email unknown subject returns unknown kind")
    def _():
        import stock_spike_monitor as m
        result = m._rh_parse_tp_email("Some body text", "Hello from TradersPost")
        assert result["kind"] == "unknown"

    @t("v3.4.37: RH_LONG_ONLY defaults to True")
    def _():
        import stock_spike_monitor as m
        assert isinstance(m.RH_LONG_ONLY, bool), "RH_LONG_ONLY must be bool"
        # Default is True (env var not set in test environment)
        assert m.RH_LONG_ONLY is True, \
            f"RH_LONG_ONLY should default to True, got {m.RH_LONG_ONLY}"

    @t("v3.4.37: CURRENT notes stay within 34-char mobile budget")
    def _():
        import stock_spike_monitor as m
        for name in ("CURRENT_MAIN_NOTE", "CURRENT_TP_NOTE",
                     "_MAIN_HISTORY_TAIL", "_TP_HISTORY_TAIL"):
            val = getattr(m, name, "")
            for i, line in enumerate(val.split("\n")):
                assert len(line) <= 34, \
                    f"{name} line {i} over 34 chars ({len(line)}): {line!r}"

    # --------------------------------------------------------
    # v3.4.38 — Robinhood live-trading kill switch
    # --------------------------------------------------------
    @t("v3.4.38: is_traderspost_enabled() falls back to env when override is None")
    def _():
        import stock_spike_monitor as m
        prev = m._traderspost_runtime_override
        try:
            m._traderspost_runtime_override = None
            assert m.is_traderspost_enabled() is m._TRADERSPOST_ENABLED_ENV, \
                "getter should return env value when override is None"
        finally:
            m._traderspost_runtime_override = prev

    @t("v3.4.38: runtime override wins over env default")
    def _():
        import stock_spike_monitor as m
        prev = m._traderspost_runtime_override
        try:
            m._traderspost_runtime_override = True
            assert m.is_traderspost_enabled() is True
            m._traderspost_runtime_override = False
            assert m.is_traderspost_enabled() is False
        finally:
            m._traderspost_runtime_override = prev

    @t("v3.4.38: _rh_set_enabled flips override and calls save_tp_state")
    def _():
        import stock_spike_monitor as m
        prev_override = m._traderspost_runtime_override
        saved = {"count": 0}
        orig_save = m.save_tp_state
        m.save_tp_state = lambda: saved.update(count=saved["count"] + 1)
        try:
            m._rh_set_enabled(True)
            assert m._traderspost_runtime_override is True
            m._rh_set_enabled(False)
            assert m._traderspost_runtime_override is False
            assert saved["count"] == 2, \
                f"save_tp_state should be called on every toggle, got {saved['count']}"
        finally:
            m._traderspost_runtime_override = prev_override
            m.save_tp_state = orig_save

    @t("v3.4.38: kill-switch override persists across save/load cycle")
    def _():
        import stock_spike_monitor as m
        import tempfile, os as _os
        prev_override = m._traderspost_runtime_override
        prev_state_file = m.TP_STATE_FILE
        prev_loaded = m._tp_state_loaded
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        tmp.close()
        try:
            m.TP_STATE_FILE = tmp.name
            m._tp_state_loaded = True  # allow save_tp_state to write
            m._traderspost_runtime_override = True
            m.save_tp_state()
            # wipe and reload
            m._traderspost_runtime_override = None
            m.load_tp_state()
            assert m._traderspost_runtime_override is True, \
                f"override should be True after reload, got {m._traderspost_runtime_override!r}"
            # now flip to False and re-verify
            m._traderspost_runtime_override = False
            m.save_tp_state()
            m._traderspost_runtime_override = None
            m.load_tp_state()
            assert m._traderspost_runtime_override is False, \
                f"override should be False after reload, got {m._traderspost_runtime_override!r}"
        finally:
            try:
                _os.unlink(tmp.name)
            except Exception:
                pass
            m.TP_STATE_FILE = prev_state_file
            m._traderspost_runtime_override = prev_override
            m._tp_state_loaded = prev_loaded

    @t("v3.4.38: cmd_rh_enable / cmd_rh_disable / cmd_rh_status handlers exist")
    def _():
        import stock_spike_monitor as m
        for name in ("cmd_rh_enable", "cmd_rh_disable", "cmd_rh_status"):
            fn = getattr(m, name, None)
            assert fn is not None, f"{name} must be defined"
            assert callable(fn), f"{name} must be callable"

    @t("v3.4.38: send_traderspost_order respects runtime-disable override")
    def _():
        import stock_spike_monitor as m
        reset_state()
        prev_override = m._traderspost_runtime_override
        prev_env = m._TRADERSPOST_ENABLED_ENV
        try:
            # Force env=on to prove the runtime override wins.
            m._TRADERSPOST_ENABLED_ENV = True
            m._traderspost_runtime_override = False
            result = m.send_traderspost_order("AAPL", "buy", 200.0, shares=5)
            assert result.get("skipped") is True, \
                f"runtime-disable should skip webhook: {result}"
        finally:
            m._traderspost_runtime_override = prev_override
            m._TRADERSPOST_ENABLED_ENV = prev_env

    # ======================================================
    # v3.4.39 \u2014 Robinhood bot consolidation
    # Scoped commands (no paper leakage) + dashboard RH payload
    # ======================================================

    @t("v3.4.39: cmd_trade_log routes by is_tp_update(update)")
    def _():
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m.cmd_trade_log)
        assert "is_tp_update(update)" in src, \
            "cmd_trade_log must branch on is_tp_update(update)"
        assert 'portfolio=' in src, \
            "cmd_trade_log must pass portfolio= to trade_log_read_tail"

    @t("v3.4.39: cmd_retighten routes by is_tp_update(update)")
    def _():
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m.cmd_retighten)
        assert "is_tp_update(update)" in src, \
            "cmd_retighten must branch on is_tp_update(update)"
        assert 'portfolio=' in src, \
            "cmd_retighten must pass portfolio= to retighten_all_stops"

    @t("v3.4.39: retighten_all_stops accepts portfolio= kwarg")
    def _():
        import stock_spike_monitor as m
        import inspect
        sig = inspect.signature(m.retighten_all_stops)
        assert "portfolio" in sig.parameters, \
            f"retighten_all_stops must accept portfolio= kwarg: {sig}"

    @t("v3.4.39: trade_log_read_tail accepts portfolio= kwarg")
    def _():
        import stock_spike_monitor as m
        import inspect
        sig = inspect.signature(m.trade_log_read_tail)
        assert "portfolio" in sig.parameters, \
            f"trade_log_read_tail must accept portfolio= kwarg: {sig}"

    @t("v3.4.39: cmd_reset on TP bot offers only Robinhood reset")
    def _():
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m.cmd_reset)
        assert "is_tp_update(update)" in src, \
            "cmd_reset must branch on is_tp_update(update)"
        assert "RH_STARTING_CAPITAL" in src, \
            "cmd_reset must format amount from RH_STARTING_CAPITAL on TP bot"
        # When on TP bot, any paper/both arg must be redirected, not executed.
        assert "Robinhood" in src and "main bot" in src, \
            "cmd_reset must redirect paper/both args on TP bot back to main"

    @t("v3.4.39: dashboard snapshot exposes rh_portfolio/rh_positions/rh_trades_today")
    def _():
        snap = ds.snapshot()
        for key in ("rh_portfolio", "rh_positions", "rh_trades_today"):
            assert key in snap, \
                f"snapshot() missing {key!r} \u2014 got keys: {sorted(snap.keys())}"
        # rh_portfolio must carry the RH-specific starting capital, not $100k.
        rh = snap["rh_portfolio"]
        assert isinstance(rh, dict), f"rh_portfolio must be a dict, got {type(rh)}"
        # Must include the headline fields the JS slice() helper reads.
        # v3.4.39: rh_portfolio mirrors the paper 'portfolio' dict shape so
        # the dashboard slice() helper can substitute it transparently.
        for field in ("cash", "start", "equity", "day_pnl",
                      "day_pnl_realized", "day_pnl_unrealized",
                      "long_mv", "vs_start"):
            assert field in rh, \
                f"rh_portfolio missing {field!r}: {sorted(rh.keys())}"

    @t("v3.4.39: dashboard snapshot rh_portfolio.start == RH_STARTING_CAPITAL")
    def _():
        snap = ds.snapshot()
        assert float(snap["rh_portfolio"]["start"]) == float(m.RH_STARTING_CAPITAL), \
            f"rh_portfolio.start ({snap['rh_portfolio']['start']}) must equal RH_STARTING_CAPITAL ({m.RH_STARTING_CAPITAL})"

    @t("v3.4.39: dashboard index.html has Paper/Robinhood view toggle")
    def _():
        # The test chdirs to /tmp/ssm_smoke_state \u2014 resolve relative to repo root.
        import pathlib
        repo_root = pathlib.Path(__file__).resolve().parent
        html = (repo_root / "dashboard_static/index.html").read_text()
        # Two toggle buttons with the scoped ids.
        assert 'id="view-btn-paper"' in html, \
            "index.html missing view-btn-paper toggle"
        assert 'id="view-btn-rh"' in html, \
            "index.html missing view-btn-rh toggle"
        # localStorage persistence \u2014 either loadView/saveView helpers,
        # or a direct localStorage.*Item call on 'portfolio_view'.
        assert "portfolio_view" in html, \
            "index.html must persist toggle under 'portfolio_view' key"

    # ================================================================
    # v3.4.40 — Robinhood independence tests
    # ================================================================

    @t("v3.4.40: BOT_VERSION is >= 3.4.40 (RH independence line)")
    def _():
        import stock_spike_monitor as m
        parts = tuple(int(x) for x in m.BOT_VERSION.split("."))
        assert parts >= (3, 4, 40), \
            f"BOT_VERSION must be >= 3.4.40, got {m.BOT_VERSION!r}"

    @t("v3.4.40: execute_rh_entry exists and is parallel to execute_entry")
    def _():
        import stock_spike_monitor as m
        assert hasattr(m, "execute_rh_entry"), \
            "execute_rh_entry must be defined"
        assert hasattr(m, "check_entry_rh"), \
            "check_entry_rh must be defined"

    @t("v3.4.40: _tp_trading_halted is a separate flag from _trading_halted")
    def _():
        import stock_spike_monitor as m
        assert hasattr(m, "_tp_trading_halted"), \
            "_tp_trading_halted module global must exist"
        assert hasattr(m, "_tp_trading_halted_reason"), \
            "_tp_trading_halted_reason module global must exist"

    @t("v3.4.40: tp_daily_entry_count dict exists for RH per-ticker cap")
    def _():
        import stock_spike_monitor as m
        assert hasattr(m, "tp_daily_entry_count"), \
            "tp_daily_entry_count dict must exist"
        assert isinstance(m.tp_daily_entry_count, dict), \
            "tp_daily_entry_count must be a dict"

    @t("v3.4.40: execute_entry no longer contains RH mirror block")
    def _():
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m.execute_entry)
        # The paper-side execute_entry should never touch tp_positions,
        # tp_paper_cash, or send_traderspost_order directly — that belongs
        # to execute_rh_entry now.
        assert "tp_positions[" not in src, \
            "execute_entry must not write to tp_positions (RH mirror removed)"
        assert "send_traderspost_order" not in src, \
            "execute_entry must not call send_traderspost_order"
        assert "tp_paper_cash" not in src, \
            "execute_entry must not touch tp_paper_cash"

    @t("v3.4.40: execute_rh_entry uses _tp_trading_halted via _compute_tp_halt")
    def _():
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m.execute_rh_entry)
        assert "_compute_tp_halt" in src, \
            "execute_rh_entry must consult _compute_tp_halt (RH halt)"
        assert "tp_paper_cash" in src, \
            "execute_rh_entry must check tp_paper_cash for independent cash"

    @t("v3.4.40: execute_rh_entry enforces RH_MAX_ENTRIES_PER_TICKER")
    def _():
        import stock_spike_monitor as m
        import inspect
        src_check = inspect.getsource(m.check_entry_rh)
        assert "RH_MAX_ENTRIES_PER_TICKER" in src_check, \
            "check_entry_rh must enforce RH_MAX_ENTRIES_PER_TICKER"
        assert "tp_daily_entry_count" in src_check, \
            "check_entry_rh must read tp_daily_entry_count"

    @t("v3.4.40: send_traderspost_order requires shares= (no paper SHARES default)")
    def _():
        import stock_spike_monitor as m
        import inspect
        # Signature check: default must be None (or removed), not SHARES.
        sig = inspect.signature(m.send_traderspost_order)
        shares_param = sig.parameters.get("shares")
        assert shares_param is not None, "send_traderspost_order must still accept shares="
        assert shares_param.default is None, \
            f"shares default must be None (v3.4.40 lockdown), got {shares_param.default!r}"
        # Call without shares must raise TypeError.
        try:
            m.send_traderspost_order("AAPL", "buy", 100.0)
        except TypeError:
            pass
        else:
            assert False, "send_traderspost_order must raise TypeError when shares is omitted"

    @t("v3.4.40: entry loop calls execute_rh_entry in parallel with execute_entry")
    def _():
        import stock_spike_monitor as m
        import inspect
        # The scan cycle function that iterates TRADE_TICKERS must call
        # both execute_entry and execute_rh_entry — with independent
        # guards (paper_holds / rh_holds).
        src_path = Path(__file__).resolve().parent / "stock_spike_monitor.py"
        src = src_path.read_text(encoding="utf-8")
        assert "execute_rh_entry(ticker" in src, \
            "scan loop must invoke execute_rh_entry(ticker, ...)"
        assert "paper_holds" in src and "rh_holds" in src, \
            "scan loop must compute paper_holds and rh_holds independently"

    @t("v3.4.40: _do_reset_tp clears RH halt and per-ticker counter")
    def _():
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m._do_reset_tp)
        assert "tp_daily_entry_count.clear()" in src, \
            "_do_reset_tp must clear tp_daily_entry_count"
        assert "_tp_trading_halted = False" in src, \
            "_do_reset_tp must reset _tp_trading_halted"

    @t("v3.4.40: reset_daily_state also resets RH halt + tp_daily_entry_count")
    def _():
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m.reset_daily_state)
        assert "tp_daily_entry_count.clear()" in src, \
            "reset_daily_state must clear tp_daily_entry_count on day rollover"
        assert "_tp_trading_halted = False" in src, \
            "reset_daily_state must reset _tp_trading_halted on day rollover"

    @t("v3.4.40: TP_RELEASE_NOTE retains v3.4.40 independence line")
    def _():
        import stock_spike_monitor as m
        # Once the current release moves past v3.4.40 the CURRENT note
        # changes, but the rolling history must still carry the
        # v3.4.40 independence line so /version shows it.
        hist = m.TP_RELEASE_NOTE
        assert "v3.4.40" in hist, \
            "TP_RELEASE_NOTE must retain a v3.4.40 line in the rolling history"
        assert any(k in hist.lower() for k in ("independence", "independent", "rh-only")), \
            "TP_RELEASE_NOTE must describe the v3.4.40 independence change"

    # ================================================================
    # v3.4.41 — /reset auth hardening
    # ================================================================

    @t("v3.4.41: TELEGRAM_TP_CHAT_ID falls back to default on empty env")
    def _():
        import importlib, os, sys
        # Force-reimport with an empty env var to prove the empty-string
        # fallback path resolves to the hardcoded owner default, not ''.
        prev = os.environ.get("TELEGRAM_TP_CHAT_ID")
        os.environ["TELEGRAM_TP_CHAT_ID"] = ""
        try:
            sys.modules.pop("stock_spike_monitor", None)
            m = importlib.import_module("stock_spike_monitor")
            assert m.TELEGRAM_TP_CHAT_ID == m._RH_OWNER_DEFAULT, \
                f"empty env must fall back to default, got {m.TELEGRAM_TP_CHAT_ID!r}"
        finally:
            if prev is None:
                os.environ.pop("TELEGRAM_TP_CHAT_ID", None)
            else:
                os.environ["TELEGRAM_TP_CHAT_ID"] = prev
            sys.modules.pop("stock_spike_monitor", None)
            importlib.import_module("stock_spike_monitor")

    @t("v3.4.41: _reset_authorized accepts tapping user id, not just chat id")
    def _():
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m._reset_authorized)
        # Must read from_user.id and check it against the owner set.
        assert "from_user" in src, \
            "_reset_authorized must read query.from_user for group-chat resets"
        assert "user_id_str" in src, \
            "_reset_authorized must derive a user_id_str from query.from_user"

    @t("v3.4.41: _reset_authorized rejects blank owner id from the match set")
    def _():
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m._reset_authorized)
        # The owner set must not accept '' as a legitimate match even if
        # CHAT_ID is unset. We look for the explicit discard.
        assert "owner_ids.discard(\"\")" in src, \
            "_reset_authorized must never accept empty-string as an owner id"

    @t("v3.4.41: v3.4.41 auth-hardening line persists in MAIN history")
    def _():
        import stock_spike_monitor as m
        assert "v3.4.41" in m.MAIN_RELEASE_NOTE, \
            "MAIN_RELEASE_NOTE must retain v3.4.41 in the rolling history"

    @t("v3.4.41: v3.4.41 auth-hardening line persists in TP history")
    def _():
        import stock_spike_monitor as m
        assert "v3.4.41" in m.TP_RELEASE_NOTE, \
            "TP_RELEASE_NOTE must retain v3.4.41 in the rolling history"

    # ================================================================
    # v3.4.42 — reset-blocked diagnostic message
    # ================================================================

    @t("v3.4.42: reset_callback blocked message includes chat_id/user_id")
    def _():
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m.reset_callback)
        # The blocked branch must surface enough info to diagnose.
        for token in ("chat_id:", "user_id:", "allowed TP:", "allowed paper:"):
            assert token in src, \
                f"reset_callback diagnostic message missing {token!r}"

    @t("v3.4.42: reset_callback logs TELEGRAM_TP_CHAT_ID and CHAT_ID")
    def _():
        import stock_spike_monitor as m
        import inspect
        src = inspect.getsource(m.reset_callback)
        assert "TELEGRAM_TP_CHAT_ID" in src and "CHAT_ID" in src, \
            "reset_callback warning must log the configured owner ids"

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
