#!/usr/bin/env python3
"""
smoke_test.py  —  Smoke test for TradeGenius (v3.5.1 paper-only).

Two modes:

  python smoke_test.py --local
      Exercises bot logic in-process against the imported module with
      synthetic state. Paper-book only (v3.5.0 deletion pass removed
      TradersPost, Robinhood, Gmail/IMAP surfaces).

  python smoke_test.py --prod [--url URL] [--password PW]
      Hits the live Railway deployment.

  python smoke_test.py  (no flag)
      Runs both in sequence.

Exit codes:
  0  — all passed
  1  — one or more failed
  2  — module import or setup error
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
# Tiny test harness
# ------------------------------------------------------------

_RESULTS: list[tuple[str, bool, str]] = []
_REGISTRY: list[tuple[str, Callable[[], None]]] = []


def t(name: str) -> Callable:
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
    for name, fn in _REGISTRY:
        _execute(name, fn)
    _REGISTRY.clear()
    return _report(label)


def _report(label: str) -> int:
    width = max(len(n) for n, _, _ in _RESULTS) if _RESULTS else 40
    print(f"\n=== {label} ===")
    fails = 0
    for name, ok, detail in _RESULTS:
        marker = "+" if ok else "X"
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
# LOCAL MODE
# ============================================================

def run_local() -> int:
    os.environ["SSM_SMOKE_TEST"] = "1"
    os.environ.setdefault("CHAT_ID", "999999999")
    os.environ.setdefault("DASHBOARD_PASSWORD", "smoketest1234")
    os.environ.setdefault("TELEGRAM_TOKEN",
                          "0000000000:AAAA_smoke_placeholder_token_0000000")

    tmp_dir = Path("/tmp/ssm_smoke_state")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(tmp_dir)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import trade_genius as m  # noqa: E402
        import dashboard_server as ds    # noqa: E402
    except Exception as e:
        print(f"Module import failed: {e}")
        traceback.print_exc()
        return 2

    def reset_state() -> None:
        m.positions.clear()
        m.short_positions.clear()
        m.paper_trades.clear()
        m.trade_history.clear()
        m.short_trade_history.clear()
        m.daily_entry_count.clear()
        m.daily_short_entry_count.clear()
        m.paper_cash = m.PAPER_STARTING_CAPITAL
        m._trading_halted = False
        m._trading_halted_reason = ""

    today = m._now_et().strftime("%Y-%m-%d")

    # ---------- utility ----------
    @t("utility: _clamp respects bounds")
    def _():
        assert m._clamp(5, (0, 10)) == 5
        assert m._clamp(-1, (0, 10)) == 0
        assert m._clamp(99, (0, 10)) == 10

    @t("utility: _now_et timezone is America/New_York")
    def _():
        n = m._now_et()
        assert str(n.tzinfo) in ("America/New_York", "US/Eastern") or "New_York" in str(n.tzinfo)

    # ---------- paper book math ----------
    @t("paper: _today_pnl_breakdown sums long + short")
    def _():
        reset_state()
        m.paper_trades.append({"ticker": "A", "action": "SELL", "date": today, "pnl": 100.0})
        m.short_trade_history.append({"ticker": "B", "date": today, "pnl": -25.0})
        sells, covers, total, wins, losses, n = m._today_pnl_breakdown()
        assert len(sells) == 1 and len(covers) == 1
        assert abs(total - 75.0) < 0.01, f"got {total}"

    @t("paper: _compute_today_realized_pnl counts shorts")
    def _():
        reset_state()
        m.short_trade_history.append({"ticker": "B", "date": today, "pnl": -40.0})
        pnl = m._compute_today_realized_pnl()
        assert abs(pnl - (-40.0)) < 0.01, f"got {pnl}"

    @t("paper: state persists cash and positions")
    def _():
        reset_state()
        m.paper_cash = 12345.67
        m.positions["XYZ"] = {
            "shares": 10, "entry_price": 10.0, "stop_price": 9.0,
            "entry_time": "10:00", "date": today,
        }
        m.save_paper_state()
        m.paper_cash = 0.0
        m.positions.clear()
        m.load_paper_state()
        assert abs(m.paper_cash - 12345.67) < 0.01
        assert "XYZ" in m.positions

    # ---------- _reset_authorized ----------
    @t("reset: accepts fresh confirm from owner")
    def _():
        class Q:
            data = f"reset_paper_confirm:{int(time.time())}"
            class message:
                chat_id = int(os.environ["CHAT_ID"])
            class from_user:
                id = int(os.environ["CHAT_ID"])
        ok, reason = m._reset_authorized(Q())
        assert ok, f"expected allowed, reason={reason}"

    @t("reset: blocks unauthorized chat")
    def _():
        class Q:
            data = f"reset_paper_confirm:{int(time.time())}"
            class message:
                chat_id = 12345
            class from_user:
                id = 12345
        ok, reason = m._reset_authorized(Q())
        assert not ok and "unauthorized" in reason

    @t("reset: blocks stale confirm (>60s old)")
    def _():
        class Q:
            data = f"reset_paper_confirm:{int(time.time()) - 1000}"
            class message:
                chat_id = int(os.environ["CHAT_ID"])
            class from_user:
                id = int(os.environ["CHAT_ID"])
        ok, reason = m._reset_authorized(Q())
        assert not ok and "expired" in reason

    # ---------- EOD report ----------
    @t("eod: _build_eod_report returns a string")
    def _():
        reset_state()
        m.paper_trades.append({"ticker": "A", "action": "SELL", "date": today,
                               "pnl": 10.0, "shares": 1, "price": 10.0,
                               "time": "10:00"})
        out = m._build_eod_report(today)
        assert isinstance(out, str) and len(out) > 0

    # ---------- dashboard snapshot ----------
    @t("dashboard: snapshot returns ok=True with paper portfolio")
    def _():
        reset_state()
        snap = ds.snapshot()
        assert isinstance(snap, dict)
        assert snap.get("ok") is True, f"snapshot not ok: {snap}"
        assert "portfolio" in snap
        # v3.5.0 — must NOT expose TP keys
        for bad in ("tp_sync", "rh_portfolio", "rh_positions", "rh_trades_today"):
            assert bad not in snap, f"v3.5.0: {bad} should be removed"

    # ---------- version ----------
    @t("version: BOT_NAME is TradeGenius")
    def _():
        assert getattr(m, "BOT_NAME", None) == "TradeGenius", \
            f"got {getattr(m, 'BOT_NAME', None)!r}"

    @t("version: BOT_VERSION is 3.5.1")
    def _():
        assert m.BOT_VERSION == "3.5.1", f"got {m.BOT_VERSION}"

    @t("version: CURRENT_MAIN_NOTE begins with v3.5.1")
    def _():
        assert m.CURRENT_MAIN_NOTE.lstrip().startswith("v3.5.1"), \
            f"note starts: {m.CURRENT_MAIN_NOTE[:40]!r}"

    @t("version: no TP/RH surfaces in module")
    def _():
        for bad in ("tp_positions", "tp_paper_cash", "tp_trade_history",
                    "tp_short_positions", "tp_short_trade_history",
                    "tp_unsynced_exits", "tp_state", "tp_dm_chat_id",
                    "_tp_trading_halted", "_tp_save_lock", "_tp_state_loaded",
                    "save_tp_state", "load_tp_state", "send_tp_telegram",
                    "send_traderspost_order", "manage_tp_positions",
                    "execute_rh_entry", "rh_imap_poll_once",
                    "cmd_tp_sync", "cmd_rh_enable", "cmd_rh_disable",
                    "cmd_rh_status", "is_traderspost_enabled", "is_tp_update",
                    "check_entry_rh", "RH_STARTING_CAPITAL", "RH_IMAP_ENABLED",
                    "GMAIL_ADDRESS", "TELEGRAM_TP_TOKEN"):
            assert not hasattr(m, bad), f"v3.5.0: {bad} should be removed"

    return run_suite("LOCAL SMOKE TESTS (v3.5.1 paper-only)")


# ============================================================
# PROD MODE
# ============================================================

def run_prod(url: str, password: str, expected_version: str | None) -> int:
    try:
        import requests
    except ImportError:
        print("prod mode requires `pip install requests`")
        return 2

    url = url.rstrip("/")
    sess = requests.Session()

    @t("prod: /login with correct password returns 302")
    def _():
        r = sess.post(f"{url}/login", data={"password": password},
                      allow_redirects=False, timeout=10)
        assert r.status_code == 302, f"expected 302, got {r.status_code}"
        cookie = sess.cookies.get("spike_session")
        assert cookie and ":" in cookie, f"bad cookie format: {cookie}"

    @t("prod: /login with wrong password returns 401")
    def _():
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
        assert "version" in data
        if expected_version:
            assert data["version"] == expected_version
        print(f"  live version: {data['version']}")

    @t("prod: /api/state exposes expected paper-only keys")
    def _():
        r = sess.get(f"{url}/api/state", timeout=10)
        data = r.json()
        needed = {"version", "portfolio", "positions", "regime", "tickers"}
        missing = needed - set(data.keys())
        assert not missing, f"missing keys: {missing}"
        # v3.5.0 — these must NOT be present
        for bad in ("tp_sync", "rh_portfolio", "rh_positions", "rh_trades_today"):
            assert bad not in data, f"v3.5.0: {bad} should be removed"

    @t("prod: /api/state rejects request with no cookie")
    def _():
        s3 = requests.Session()
        r = s3.get(f"{url}/api/state", allow_redirects=False, timeout=10)
        assert r.status_code in (302, 401, 403)

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

    @t("prod: rate limiter trips on 6th bad attempt")
    def _():
        s5 = requests.Session()
        statuses = []
        for i in range(7):
            r = s5.post(f"{url}/login",
                        data={"password": "wrong-rate-limit-test"},
                        allow_redirects=False, timeout=10)
            statuses.append(r.status_code)
            time.sleep(0.3)
        assert 429 in statuses[5:], \
            f"rate limit never tripped; statuses={statuses}"

    return run_suite("PROD SMOKE TESTS")


# ============================================================
# CLI
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="TradeGenius smoke test")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--prod", action="store_true")
    parser.add_argument("--url",
                        default="https://stock-spike-monitor-production.up.railway.app")
    parser.add_argument("--password",
                        default=os.environ.get("DASHBOARD_PASSWORD", ""))
    parser.add_argument("--expected-version", default=None)
    args = parser.parse_args()

    do_local = args.local or not (args.local or args.prod)
    do_prod = args.prod or not (args.local or args.prod)

    total_fails = 0
    if do_local:
        total_fails += run_local()
    if do_prod:
        if not args.password:
            print("(prod mode skipped — no --password)")
        else:
            total_fails += run_prod(args.url, args.password, args.expected_version)

    print(f"=== RESULT: {'PASS' if total_fails == 0 else f'FAIL ({total_fails})'} ===")
    return 0 if total_fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
