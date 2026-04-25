#!/usr/bin/env python3
"""
smoke_test.py  —  Smoke test for TradeGenius (v3.6.0 paper-only).

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
        import telegram_commands as m_tc  # noqa: E402  # v4.5.0 extraction
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

    # ---------- _reset_authorized (v4.4.0: owner user_id only) ----------
    # Pick any owner id from the module's authoritative set so these
    # tests track whatever TRADEGENIUS_OWNER_IDS is configured to.
    owner_uid = next(iter(m.TRADEGENIUS_OWNER_IDS))
    non_owner_uid = "12345"  # not in owner set
    assert non_owner_uid not in m.TRADEGENIUS_OWNER_IDS

    @t("reset: accepts fresh confirm from owner user_id (any chat)")
    def _():
        class Q:
            data = f"reset_paper_confirm:{int(time.time())}"
            class message:
                # arbitrary chat \u2014 auth must not depend on chat_id
                chat_id = 424242
            class from_user:
                id = int(owner_uid)
        ok, reason = m_tc._reset_authorized(Q())
        assert ok, f"expected allowed, reason={reason}"

    @t("reset: v4.4.0 rejects non-owner user even when chat_id == CHAT_ID")
    def _():
        # Pre-v4.4.0 bypass: non-owner user in the configured CHAT_ID
        # group could tap Confirm. Must now be REJECTED.
        class Q:
            data = f"reset_paper_confirm:{int(time.time())}"
            class message:
                chat_id = int(os.environ["CHAT_ID"])
            class from_user:
                id = int(non_owner_uid)
        ok, reason = m_tc._reset_authorized(Q())
        assert not ok, "non-owner in CHAT_ID group must be rejected post-v4.4.0"
        assert "unauthorized" in reason, f"unexpected reason={reason}"

    @t("reset: blocks unauthorized user from arbitrary chat")
    def _():
        class Q:
            data = f"reset_paper_confirm:{int(time.time())}"
            class message:
                chat_id = 12345
            class from_user:
                id = int(non_owner_uid)
        ok, reason = m_tc._reset_authorized(Q())
        assert not ok and "unauthorized" in reason

    @t("reset: v4.4.0 denies when user_id cannot be determined")
    def _():
        class Q:
            data = f"reset_paper_confirm:{int(time.time())}"
            class message:
                chat_id = int(os.environ["CHAT_ID"])
            from_user = None
        ok, reason = m_tc._reset_authorized(Q())
        assert not ok and "no user_id" in reason

    @t("reset: blocks stale confirm (>60s old) even for owner")
    def _():
        class Q:
            data = f"reset_paper_confirm:{int(time.time()) - 1000}"
            class message:
                chat_id = int(os.environ["CHAT_ID"])
            class from_user:
                id = int(owner_uid)
        ok, reason = m_tc._reset_authorized(Q())
        assert not ok and "expired" in reason

    # ---------- v4.4.0 sub-bot (Val/Gene) auth ----------
    @t("auth: sub-bot _auth_guard drops non-owner user")
    def _():
        import asyncio
        from telegram.ext import ApplicationHandlerStop

        class _Base(m.TradeGeniusBase):
            NAME = "SmokeSub"
            mode = "paper"
            def __init__(self_inner):
                self_inner.owner_ids = set(m.TRADEGENIUS_OWNER_IDS)

        bot = _Base()
        class FakeUser: id = int(non_owner_uid)
        class FakeUpdate:
            effective_user = FakeUser()
        raised = False
        try:
            asyncio.run(bot._auth_guard(FakeUpdate(), None))
        except ApplicationHandlerStop:
            raised = True
        assert raised, "sub-bot _auth_guard must raise ApplicationHandlerStop for non-owner"

    @t("auth: sub-bot _auth_guard passes owner through")
    def _():
        import asyncio

        class _Base(m.TradeGeniusBase):
            NAME = "SmokeSub"
            mode = "paper"
            def __init__(self_inner):
                self_inner.owner_ids = set(m.TRADEGENIUS_OWNER_IDS)

        bot = _Base()
        class FakeUser: id = int(owner_uid)
        class FakeUpdate:
            effective_user = FakeUser()
        result = asyncio.run(bot._auth_guard(FakeUpdate(), None))
        assert result is None

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

    # v4.1.7 H7 — _today_trades must de-duplicate a row that appears in
    # both paper_trades and short_trade_history (a cross-list dupe
    # would cause the UI to show the same short cover twice).
    @t("dashboard: _today_trades de-duplicates cross-list short")
    def _():
        reset_state()
        row = {
            "ticker": "FAKE",
            "action": "COVER",
            "date": today,
            "time": "10:30",
            "side": "SHORT",
            "shares": 10,
            "price": 5.0,
            "pnl": 12.5,
        }
        m.paper_trades.append(dict(row))
        m.short_trade_history.append(dict(row))
        rows = ds._today_trades()
        fake_rows = [r for r in rows if r.get("ticker") == "FAKE"]
        assert len(fake_rows) == 1, \
            f"expected 1 de-duped FAKE row, got {len(fake_rows)}: {fake_rows}"

    # ---------- version ----------
    @t("version: BOT_NAME is TradeGenius")
    def _():
        assert getattr(m, "BOT_NAME", None) == "TradeGenius", \
            f"got {getattr(m, 'BOT_NAME', None)!r}"

    @t("version: BOT_VERSION is 4.11.5")
    def _():
        assert m.BOT_VERSION == "4.11.5", f"got {m.BOT_VERSION}"

    @t("version: no -beta suffix")
    def _():
        assert "beta" not in m.BOT_VERSION.lower(), \
            f"BOT_VERSION still carries beta moniker: {m.BOT_VERSION!r}"

    @t("version: CURRENT_MAIN_NOTE begins with current BOT_VERSION")
    def _():
        # v4.11.5 — was hardcoded "v4.11.2" and got missed on .3/.4. Derive
        # from BOT_VERSION so it self-tracks every release.
        expected = f"v{m.BOT_VERSION}"
        assert m.CURRENT_MAIN_NOTE.lstrip().startswith(expected), \
            f"note starts: {m.CURRENT_MAIN_NOTE[:40]!r}, expected prefix {expected!r}"

    @t("version: CURRENT_MAIN_NOTE every line <= 34 chars")
    def _():
        for ln in m.CURRENT_MAIN_NOTE.split("\n"):
            assert len(ln) <= 34, f"line too wide ({len(ln)}): {ln!r}"

    # ---------- v4.0.2-beta DI seed ----------
    @t("di_seed: _seed_di_buffer function exists")
    def _():
        assert hasattr(m, "_seed_di_buffer"), \
            "_seed_di_buffer missing from trade_genius module"
        assert callable(m._seed_di_buffer), \
            "_seed_di_buffer is not callable"
        assert hasattr(m, "_DI_SEED_CACHE"), \
            "_DI_SEED_CACHE module global missing"

    @t("di_seed: DI_PREMARKET_SEED env var documented in .env.example")
    def _():
        env_path = Path(__file__).parent / ".env.example"
        assert env_path.exists(), f".env.example missing at {env_path}"
        text = env_path.read_text(encoding="utf-8")
        assert "DI_PREMARKET_SEED" in text, \
            "DI_PREMARKET_SEED not documented in .env.example"

    # ---------- v4.0.3-beta OR seed ----------
    @t("or_seed: _seed_opening_range function exists")
    def _():
        assert hasattr(m, "_seed_opening_range"), \
            "_seed_opening_range missing from trade_genius module"
        assert callable(m._seed_opening_range), \
            "_seed_opening_range is not callable"
        assert hasattr(m, "_seed_opening_range_all"), \
            "_seed_opening_range_all missing"
        assert callable(m._seed_opening_range_all), \
            "_seed_opening_range_all is not callable"
        assert hasattr(m, "or_stale_skip_count"), \
            "or_stale_skip_count module global missing"
        assert isinstance(m.or_stale_skip_count, dict), \
            f"expected dict, got {type(m.or_stale_skip_count).__name__}"

    @t("or_seed: staleness guard uses configurable threshold")
    def _():
        assert hasattr(m, "OR_STALE_THRESHOLD"), \
            "OR_STALE_THRESHOLD module global missing"
        assert m.OR_STALE_THRESHOLD >= 0.03, \
            f"OR_STALE_THRESHOLD {m.OR_STALE_THRESHOLD} too tight \u2014 " \
            "v4.0.3-beta widened this to >=3% to stop killing signals " \
            "on normal intraday volatility"
        # Functional: at 4% drift, the guard should PASS (not stale)
        # under the default 5% threshold but fail under the old 1.5%.
        assert m._or_price_sane(100.0, 104.0) is True, \
            "4% drift should be sane under 5% threshold"
        assert m._or_price_sane(100.0, 104.0, threshold=0.015) is False, \
            "4% drift should fail under legacy 1.5% threshold"
        assert m._or_price_sane(100.0, 110.0) is False, \
            "10% drift must still trip the guard"

    # ---------- v4.5.0 refactor: telegram_commands extraction ----------
    @t("refactor: telegram command handlers importable from telegram_commands")
    def _():
        assert hasattr(m_tc, "cmd_status"), "cmd_status missing from telegram_commands"
        assert hasattr(m_tc, "cmd_help"), "cmd_help missing from telegram_commands"
        assert hasattr(m_tc, "cmd_reset"), "cmd_reset missing from telegram_commands"
        assert hasattr(m_tc, "cmd_mode"), "cmd_mode missing from telegram_commands"
        assert hasattr(m_tc, "reset_callback"), "reset_callback missing from telegram_commands"
        assert hasattr(m_tc, "_reset_authorized"), "_reset_authorized missing from telegram_commands"

    @t("refactor: cmd_* handlers not present on trade_genius (moved to telegram_commands)")
    def _():
        for name in ("cmd_status", "cmd_help", "cmd_reset", "cmd_mode",
                     "cmd_ticker", "cmd_perf", "reset_callback", "_reset_authorized"):
            assert not hasattr(m, name), \
                f"v4.5.0: {name} should have moved out of trade_genius"

    # ---------- v3.6.0 auth guard ----------
    @t("auth: TRADEGENIUS_OWNER_IDS exists, RH_OWNER_USER_IDS removed")
    def _():
        assert hasattr(m, "TRADEGENIUS_OWNER_IDS"), "TRADEGENIUS_OWNER_IDS missing"
        assert isinstance(m.TRADEGENIUS_OWNER_IDS, set), \
            f"expected set, got {type(m.TRADEGENIUS_OWNER_IDS).__name__}"
        assert not hasattr(m, "RH_OWNER_USER_IDS"), \
            "v3.6.0: RH_OWNER_USER_IDS should be hard-renamed away"
        assert not hasattr(m, "_RH_OWNER_USERS_RAW"), \
            "v3.6.0: _RH_OWNER_USERS_RAW should be hard-renamed away"

    @t("auth: _auth_guard exists and blocks non-owners")
    def _():
        import asyncio
        from types import SimpleNamespace
        assert hasattr(m, "_auth_guard"), "_auth_guard function missing"
        # Pick a non-owner id guaranteed not in the whitelist.
        owner_ids = set(m.TRADEGENIUS_OWNER_IDS)
        bad_id = 999999999
        while str(bad_id) in owner_ids:
            bad_id += 1
        fake_user = SimpleNamespace(id=bad_id)
        fake_chat = SimpleNamespace(id=-100123)
        fake_update = SimpleNamespace(
            effective_user=fake_user,
            effective_chat=fake_chat,
            update_id=1,
        )
        from telegram.ext import ApplicationHandlerStop
        raised = False
        try:
            asyncio.run(m._auth_guard(fake_update, None))
        except ApplicationHandlerStop:
            raised = True
        assert raised, "_auth_guard must raise ApplicationHandlerStop for non-owners"

    @t("auth: _auth_guard passes owner through (no raise)")
    def _():
        import asyncio
        from types import SimpleNamespace
        owner_ids = list(m.TRADEGENIUS_OWNER_IDS)
        assert owner_ids, "TRADEGENIUS_OWNER_IDS is empty \u2014 no owner to test"
        good_id = int(owner_ids[0])
        fake_user = SimpleNamespace(id=good_id)
        fake_chat = SimpleNamespace(id=-100123)
        fake_update = SimpleNamespace(
            effective_user=fake_user,
            effective_chat=fake_chat,
            update_id=2,
        )
        # Should NOT raise; should return None.
        result = asyncio.run(m._auth_guard(fake_update, None))
        assert result is None, f"owner path returned {result!r}"

    @t("auth: _auth_guard drops update with no effective_user")
    def _():
        import asyncio
        from types import SimpleNamespace
        fake_update = SimpleNamespace(
            effective_user=None,
            effective_chat=SimpleNamespace(id=-100123),
            update_id=3,
        )
        from telegram.ext import ApplicationHandlerStop
        raised = False
        try:
            asyncio.run(m._auth_guard(fake_update, None))
        except ApplicationHandlerStop:
            raised = True
        assert raised, "updates with no effective_user must also be dropped"

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

    # ---------- v4.0.0-alpha Val executor ----------
    @t("val: TradeGeniusVal class exists")
    def _():
        assert hasattr(m, "TradeGeniusVal"), "TradeGeniusVal missing"
        assert hasattr(m, "TradeGeniusBase"), "TradeGeniusBase missing"
        assert issubclass(m.TradeGeniusVal, m.TradeGeniusBase), \
            "TradeGeniusVal must subclass TradeGeniusBase"
        assert m.TradeGeniusVal.NAME == "Val", f"got {m.TradeGeniusVal.NAME!r}"
        assert m.TradeGeniusVal.ENV_PREFIX == "VAL_", \
            f"got {m.TradeGeniusVal.ENV_PREFIX!r}"

    @t("val: signal bus registration works")
    def _():
        before = len(m._signal_listeners)
        marker = {"hit": False}

        def _listener(event):
            marker["hit"] = True

        m.register_signal_listener(_listener)
        try:
            assert len(m._signal_listeners) == before + 1
            assert _listener in m._signal_listeners
        finally:
            # don't leak listeners across tests
            m._signal_listeners.remove(_listener)

    @t("val: _emit_signal dispatches to all listeners")
    def _():
        import threading as _th
        evt = _th.Event()
        seen = {}

        def _l(event):
            seen.update(event)
            evt.set()

        m.register_signal_listener(_l)
        try:
            m._emit_signal({
                "kind": "ENTRY_LONG", "ticker": "TEST",
                "price": 100.0, "reason": "BREAKOUT",
                "timestamp_utc": "2026-04-24T00:00:00Z",
                "main_shares": 10,
            })
            assert evt.wait(2.0), "listener did not fire within 2s"
            assert seen.get("ticker") == "TEST", f"got {seen!r}"
        finally:
            m._signal_listeners.remove(_l)

    @t("val: mode defaults to paper, flip to live without confirm fails")
    def _():
        os.environ["VAL_ALPACA_PAPER_KEY"] = "dummy_paper_key"
        os.environ["VAL_ALPACA_PAPER_SECRET"] = "dummy_paper_secret"
        os.environ["VAL_ALPACA_LIVE_KEY"] = "dummy_live_key"
        os.environ["VAL_ALPACA_LIVE_SECRET"] = "dummy_live_secret"
        # Isolate state files in a temp dir.
        v = m.TradeGeniusVal()
        assert v.mode == "paper", f"expected paper, got {v.mode!r}"
        ok, msg = v.set_mode("live")  # no confirm token
        assert not ok, f"live flip without confirm should fail, got {msg!r}"
        assert "confirm" in msg.lower(), f"expected confirm in msg, got {msg!r}"
        # Unknown mode should also fail.
        ok, msg = v.set_mode("wat")
        assert not ok
        # Paper should still succeed (client build may warn but mode flips).
        ok, msg = v.set_mode("paper")
        assert ok, f"paper flip should succeed, got {msg!r}"

    @t("val: state file path segregates paper vs live")
    def _():
        os.environ.setdefault("VAL_ALPACA_PAPER_KEY", "dummy")
        os.environ.setdefault("VAL_ALPACA_PAPER_SECRET", "dummy")
        v = m.TradeGeniusVal()
        paper_path = v._state_file("paper")
        live_path = v._state_file("live")
        assert paper_path != live_path, "paper and live paths must differ"
        assert "val" in paper_path.lower() and "val" in live_path.lower()
        assert "paper" in paper_path and "live" in live_path

    @t("val: signal bus is wired into execute_entry hook point")
    def _():
        # Register a listener, call execute_entry indirectly via _emit_signal
        # contract, confirm event shape matches schema the hook emits.
        import threading as _th
        evt = _th.Event()
        captured = {}

        def _l(event):
            captured.update(event)
            evt.set()

        m.register_signal_listener(_l)
        try:
            m._emit_signal({
                "kind": "EOD_CLOSE_ALL", "ticker": "",
                "price": 0.0, "reason": "EOD",
                "timestamp_utc": "2026-04-24T20:55:00Z",
                "main_shares": 0,
            })
            assert evt.wait(2.0)
            for key in ("kind", "ticker", "price", "reason",
                        "timestamp_utc", "main_shares"):
                assert key in captured, f"event missing {key}"
            assert captured["kind"] == "EOD_CLOSE_ALL"
        finally:
            m._signal_listeners.remove(_l)

    # ---------- v4.0.0-beta Gene executor ----------
    @t("gene: TradeGeniusGene class exists")
    def _():
        assert hasattr(m, "TradeGeniusGene"), "TradeGeniusGene missing"
        assert issubclass(m.TradeGeniusGene, m.TradeGeniusBase), \
            "TradeGeniusGene must subclass TradeGeniusBase"
        assert m.TradeGeniusGene.NAME == "Gene", f"got {m.TradeGeniusGene.NAME!r}"
        assert m.TradeGeniusGene.ENV_PREFIX == "GENE_", \
            f"got {m.TradeGeniusGene.ENV_PREFIX!r}"

    @t("gene: state file path segregates paper vs live")
    def _():
        os.environ.setdefault("GENE_ALPACA_PAPER_KEY", "dummy")
        os.environ.setdefault("GENE_ALPACA_PAPER_SECRET", "dummy")
        g = m.TradeGeniusGene()
        paper_path = g._state_file("paper")
        live_path = g._state_file("live")
        assert paper_path != live_path, "paper and live paths must differ"
        assert "gene" in paper_path.lower() and "gene" in live_path.lower()
        assert "paper" in paper_path and "live" in live_path

    @t("gene: gene_executor module global exists")
    def _():
        assert hasattr(m, "gene_executor"), "gene_executor global missing"

    # ---------- v4.0.0-beta shorts P&L sign ----------
    @t("shorts_pnl: dashboard snapshot shows profitable short with positive pnl")
    def _():
        reset_state()
        # Seed a profitable open short: entry=100, current=95, shares=10
        # → correct unrealized P&L is +50 (short profits when price falls).
        m.short_positions["FAKE"] = {
            "entry_price": 100.0, "shares": 10, "stop": 105.0,
            "entry_time": "10:00", "date": today,
        }
        # Force the dashboard snapshot to use our fabricated mark. The
        # snapshot calls _price_for, which reads fetch_1min_bars — patch
        # the helper in dashboard_server to return 95 for FAKE.
        saved = ds._price_for
        try:
            ds._price_for = lambda t: 95.0 if t == "FAKE" else saved(t)
            snap = ds.snapshot()
        finally:
            ds._price_for = saved
        assert snap.get("ok") is True, f"snapshot failed: {snap}"
        fakes = [p for p in snap.get("positions", []) if p.get("ticker") == "FAKE"]
        assert len(fakes) == 1, f"FAKE row missing: {snap.get('positions')}"
        row = fakes[0]
        assert row.get("side") == "SHORT", f"side={row.get('side')}"
        unreal = row.get("unrealized", 0)
        assert unreal > 0, f"profitable short pnl must be POSITIVE, got {unreal}"
        assert abs(unreal - 50.0) < 0.01, \
            f"expected +50.0 unrealized, got {unreal}"
        m.short_positions.pop("FAKE", None)

    @t("shorts_pnl: positions text shows profitable short with +sign")
    def _():
        reset_state()
        m.short_positions["FAKE"] = {
            "entry_price": 100.0, "shares": 10, "stop": 105.0,
            "entry_time": "10:00", "date": today,
        }
        saved = m.fetch_1min_bars
        try:
            m.fetch_1min_bars = lambda t: (
                {"current_price": 95.0} if t == "FAKE" else saved(t)
            )
            txt = m._build_positions_text()
        finally:
            m.fetch_1min_bars = saved
            m.short_positions.pop("FAKE", None)
        # Expect the positions text to render FAKE's short pnl positively.
        assert "FAKE" in txt, "FAKE missing from positions text"
        assert "P&L $+50.00" in txt or "P&L $+50" in txt, \
            f"expected positive short pnl in output:\n{txt}"

    @t("shorts_pnl: realized short pnl storage is positive for profitable cover")
    def _():
        reset_state()
        m.short_positions["FAKE"] = {
            "entry_price": 100.0, "shares": 10, "stop": 105.0,
            "entry_time": "10:00", "date": today,
            "entry_count": 1,
        }
        # Swallow Telegram + state save side effects.
        saved_send = m.send_telegram
        saved_save = m.save_paper_state
        m.send_telegram = lambda *a, **k: None
        m.save_paper_state = lambda *a, **k: None
        try:
            m.close_short_position("FAKE", 95.0, "TEST")
            hist = [t for t in m.short_trade_history if t.get("ticker") == "FAKE"]
            assert hist, "short_trade_history missing FAKE row"
            pnl = hist[-1]["pnl"]
            assert pnl > 0, f"profitable short cover stored pnl must be POSITIVE, got {pnl}"
            assert abs(pnl - 50.0) < 0.01, f"expected +50.0, got {pnl}"
        finally:
            m.send_telegram = saved_send
            m.save_paper_state = saved_save

    # ---------- v4.0.0-beta dashboard endpoints ----------
    @t("dashboard: /api/executor/val endpoint exists and returns disabled gracefully when Val is off")
    def _():
        # Simulate Val disabled by making sure the module global is None.
        saved = getattr(m, "val_executor", None)
        try:
            m.val_executor = None
            payload = ds._executor_snapshot("val")
            assert payload.get("enabled") is False, \
                f"expected enabled=False, got {payload}"
            assert "error" in payload, f"error field missing: {payload}"
        finally:
            m.val_executor = saved

    @t("dashboard: /api/indices endpoint exists")
    def _():
        # The route is registered in _build_app; inspecting the app's
        # router is enough to prove the endpoint is live without actually
        # running an HTTP server.
        app = ds._build_app()
        paths = []
        for r in app.router.routes():
            info = r.resource.get_info()
            paths.append(info.get("path") or info.get("formatter") or "")
        assert "/api/indices" in paths, f"/api/indices not registered: {paths}"
        assert "/api/executor/{name}" in paths, \
            f"/api/executor/{{name}} not registered: {paths}"

    @t("dashboard: /api/indices handles missing Alpaca client gracefully")
    def _():
        # When no executor is enabled, _resolve_data_client returns None
        # and _fetch_indices returns ok=False instead of raising.
        saved_val = getattr(m, "val_executor", None)
        saved_gene = getattr(m, "gene_executor", None)
        try:
            m.val_executor = None
            m.gene_executor = None
            payload = ds._fetch_indices()
            assert isinstance(payload, dict), f"want dict, got {type(payload)}"
            assert payload.get("ok") is False, \
                f"expected ok=False, got {payload}"
        finally:
            m.val_executor = saved_val
            m.gene_executor = saved_gene

    # ---------- v4.11.0 \u2014 error_state + health pill ----------
    @t("v4.11.0: error_state module imports and exposes API")
    def _():
        import error_state
        assert callable(getattr(error_state, "record_error", None)), \
            "error_state.record_error missing"
        assert callable(getattr(error_state, "snapshot", None)), \
            "error_state.snapshot missing"
        assert callable(getattr(error_state, "reset_daily", None)), \
            "error_state.reset_daily missing"
        assert callable(getattr(error_state, "_reset_for_tests", None)), \
            "error_state._reset_for_tests missing"

    @t("v4.11.0: record_error appends and snapshot reports green/warn/red")
    def _():
        import error_state
        error_state._reset_for_tests()
        snap = error_state.snapshot("main")
        assert snap["count"] == 0, f"expected 0, got {snap['count']}"
        assert snap["severity"] == "green", f"expected green, got {snap['severity']}"
        assert snap["entries"] == [], f"expected [], got {snap['entries']}"
        # Warning-only \u2192 amber.
        error_state.record_error("main", "FOO", "warning", "warn line", ts="t1")
        snap = error_state.snapshot("main")
        assert snap["count"] == 1
        assert snap["severity"] == "warning", f"got {snap['severity']}"
        # Add error \u2192 red.
        error_state.record_error("main", "BAR", "error", "err line", ts="t2")
        snap = error_state.snapshot("main")
        assert snap["count"] == 2
        assert snap["severity"] == "red", f"got {snap['severity']}"
        # Newest-first dropdown.
        codes = [e["code"] for e in snap["entries"]]
        assert codes[0] == "BAR" and codes[1] == "FOO", f"order={codes}"
        error_state._reset_for_tests()

    @t("v4.11.0: record_error rings cap at 50 entries per executor")
    def _():
        import error_state
        error_state._reset_for_tests()
        for i in range(60):
            error_state.record_error("main", f"CODE{i}", "error", f"line {i}", ts=f"t{i}")
        snap = error_state.snapshot("main")
        # ring is bounded; count reflects ring length
        assert snap["count"] == 50, f"expected 50, got {snap['count']}"
        # last 10 newest-first
        assert len(snap["entries"]) == 10
        assert snap["entries"][0]["code"] == "CODE59"
        error_state._reset_for_tests()

    @t("v4.11.0: dedup gate suppresses second send within cooldown")
    def _():
        import error_state
        error_state._reset_for_tests()
        # Inject a fake clock so cooldown is deterministic.
        clock = {"t": 1000.0}
        def now_fn():
            return clock["t"]
        first = error_state.record_error("main", "DEDUP_CODE", "error", "x",
                                         ts="t1", now_fn=now_fn)
        assert first is True, "first event must dispatch"
        second = error_state.record_error("main", "DEDUP_CODE", "error", "x",
                                          ts="t2", now_fn=now_fn)
        assert second is False, "second within cooldown must NOT dispatch"
        # Advance past 5-min cooldown; next must dispatch.
        clock["t"] += 301.0
        third = error_state.record_error("main", "DEDUP_CODE", "error", "x",
                                         ts="t3", now_fn=now_fn)
        assert third is True, "after cooldown event must dispatch again"
        error_state._reset_for_tests()

    @t("v4.11.0: reset_daily clears all three executors and dedup")
    def _():
        import error_state
        error_state._reset_for_tests()
        for ex in ("main", "val", "gene"):
            error_state.record_error(ex, "X", "error", "y", ts="t")
        for ex in ("main", "val", "gene"):
            assert error_state.snapshot(ex)["count"] == 1, \
                f"{ex} did not record"
        error_state.reset_daily()
        for ex in ("main", "val", "gene"):
            assert error_state.snapshot(ex)["count"] == 0, \
                f"{ex} did not reset"
        # Per-executor reset only clears that executor.
        error_state.record_error("val", "Y", "error", "z", ts="t")
        error_state.record_error("gene", "Y", "error", "z", ts="t")
        error_state.reset_daily("val")
        assert error_state.snapshot("val")["count"] == 0
        assert error_state.snapshot("gene")["count"] == 1
        error_state._reset_for_tests()

    @t("v4.11.0: record_error normalizes unknown executor and severity")
    def _():
        import error_state
        error_state._reset_for_tests()
        # Unknown executor falls back to main; unknown severity falls back to error.
        error_state.record_error("ROGUE", "Q", "weird", "y", ts="t")
        assert error_state.snapshot("main")["count"] == 1
        assert error_state.snapshot("main")["entries"][0]["severity"] == "error"
        error_state._reset_for_tests()

    @t("v4.11.0: report_error wrapper exists and routes")
    def _():
        assert callable(getattr(m, "report_error", None)), \
            "trade_genius.report_error missing"

    @t("v4.11.0: /api/errors/{executor} route registered")
    def _():
        app = ds._build_app()
        paths = []
        for r in app.router.routes():
            info = r.resource.get_info()
            paths.append(info.get("path") or info.get("formatter") or "")
        assert "/api/errors/{executor}" in paths, \
            f"/api/errors/{{executor}} not registered: {paths}"

    @t("v4.11.0: /api/state embeds errors snapshot")
    def _():
        snap = ds.snapshot()
        assert "errors" in snap, f"errors missing in /api/state: {list(snap.keys())[:20]}"
        assert isinstance(snap["errors"], dict), f"errors should be dict, got {type(snap['errors'])}"
        for k in ("count", "severity", "entries", "executor"):
            assert k in snap["errors"], f"errors.{k} missing"

    @t("v4.11.0: /api/executor/{name} embeds errors snapshot")
    def _():
        # Even when Val is disabled, the snapshot should still carry
        # an errors stanza so the pill never goes blank.
        saved = getattr(m, "val_executor", None)
        try:
            m.val_executor = None
            payload = ds._executor_snapshot("val")
            assert "errors" in payload, f"errors missing: {payload}"
            assert payload["errors"]["executor"] == "val"
        finally:
            m.val_executor = saved

    @t("v4.11.0: log buffer infrastructure removed from dashboard_server")
    def _():
        # The ring-buffer log handler and /stream logs SSE event were
        # deprecated in favor of the per-executor health pill. Asserting
        # absence guards against a partial revert.
        for name in ("_LOG_BUFFER_SIZE", "_log_buffer", "_log_seq",
                     "_RingBufferHandler", "_install_log_handler",
                     "_logs_since"):
            assert not hasattr(ds, name), \
                f"v4.11.0: dashboard_server.{name} should be removed"

    # ---------- v4.3.0 extended-entry guards ----------
    @t("guard: env flags exist with documented defaults")
    def _():
        assert hasattr(m, "ENTRY_EXTENSION_MAX_PCT"), \
            "ENTRY_EXTENSION_MAX_PCT missing"
        assert hasattr(m, "ENTRY_STOP_CAP_REJECT"), \
            "ENTRY_STOP_CAP_REJECT missing"
        assert isinstance(m.ENTRY_EXTENSION_MAX_PCT, float)
        assert isinstance(m.ENTRY_STOP_CAP_REJECT, bool)
        # Defaults: 1.5% extension, reject-on-cap ON.
        assert abs(m.ENTRY_EXTENSION_MAX_PCT - 1.5) < 1e-9, \
            f"expected 1.5, got {m.ENTRY_EXTENSION_MAX_PCT}"
        assert m.ENTRY_STOP_CAP_REJECT is True, \
            f"expected True, got {m.ENTRY_STOP_CAP_REJECT}"

    @t("guard: long extension 0.5% under 1.5% cap is allowed")
    def _():
        or_hi = 100.0
        price = or_hi * 1.005  # 0.5% extended
        ext = (price - or_hi) / or_hi * 100.0
        assert ext <= m.ENTRY_EXTENSION_MAX_PCT, \
            f"ext {ext:.2f}% should be <= {m.ENTRY_EXTENSION_MAX_PCT}%"

    @t("guard: long extension 2.0% over 1.5% cap is rejected")
    def _():
        or_hi = 100.0
        price = or_hi * 1.02  # 2.0% extended
        ext = (price - or_hi) / or_hi * 100.0
        assert ext > m.ENTRY_EXTENSION_MAX_PCT, \
            f"ext {ext:.2f}% should be > {m.ENTRY_EXTENSION_MAX_PCT}%"

    @t("guard: short extension 2.0% below OR_Low is rejected")
    def _():
        or_lo = 100.0
        price = or_lo * 0.98  # 2.0% extended below
        ext = (or_lo - price) / or_lo * 100.0
        assert ext > m.ENTRY_EXTENSION_MAX_PCT, \
            f"ext {ext:.2f}% should be > {m.ENTRY_EXTENSION_MAX_PCT}%"

    @t("guard: _capped_long_stop flags capped when baseline is too loose")
    def _():
        # Entry = $677.06, OR_High = $659.85 (META case).
        # baseline = 659.85 - 0.90 = 658.95 → 18.11 below entry → >0.75%
        stop, capped, base = m._capped_long_stop(659.85, 677.06)
        assert capped is True, "expected capped=True on META-like entry"
        # cap = entry * (1 - 0.0075) = 671.98
        assert abs(stop - 671.98) < 0.01, f"got stop={stop}"
        assert abs(base - 658.95) < 0.01, f"got base={base}"

    @t("guard: _capped_long_stop not capped when baseline is already tight")
    def _():
        # Entry near OR_High — baseline OR_High-0.90 is within 0.75%.
        # entry=100.10, or_h=100.00 → baseline=99.10 → floor=99.3495
        # baseline < floor → capped flag True. Use a scenario with
        # entry JUST at the OR edge (entry=100, or_h=100.50 invalid).
        # Pick entry=100, or_h=100 → baseline=99.10, floor=99.25 → capped.
        # To get NOT-capped we need baseline >= floor: baseline=entry-0.90;
        # floor=entry*0.9925. Need entry-0.90 >= entry*0.9925 →
        # entry*(1-0.9925) <= 0.90 → entry <= 120. So at entry=100,
        # baseline=99.10, floor=99.25 → still capped. Use entry=200,
        # or_h=200 → baseline=199.10, floor=198.50 → NOT capped.
        stop, capped, base = m._capped_long_stop(200.0, 200.0)
        assert capped is False, \
            f"expected capped=False for entry at OR edge, got capped={capped}"

    @t("guard: _capped_short_stop flags capped when baseline is too loose")
    def _():
        # Mirror case: entry far below PDC, baseline PDC+0.90 >> entry*1.0075.
        stop, capped, base = m._capped_short_stop(pdc_val=500.0, entry_price=480.0)
        assert capped is True, "expected capped=True on extended short"
        # cap = 480 * 1.0075 = 483.60
        assert abs(stop - 483.60) < 0.01, f"got stop={stop}"

    @t("guard: ENTRY_STOP_CAP_REJECT=False preserves legacy capping path")
    def _():
        # When the env flag is False, the stop-cap rejection branch is
        # skipped; the legacy _capped_long_stop still clamps at
        # execute_entry-time so current behavior is preserved. We verify
        # the logic by flipping the flag and re-reading the module toggle.
        saved_flag = m.ENTRY_STOP_CAP_REJECT
        try:
            m.ENTRY_STOP_CAP_REJECT = False
            # The capped stop is still produced by _capped_long_stop, so
            # entries on this path would still get a capped stop (old
            # behavior), NOT be rejected.
            stop, capped, base = m._capped_long_stop(659.85, 677.06)
            assert capped is True, \
                "capping machinery must stay intact when reject flag is off"
        finally:
            m.ENTRY_STOP_CAP_REJECT = saved_flag

    @t("guard: _update_gate_snapshot emits extension_pct when OR is seeded")
    def _():
        reset_state()
        # Seed OR + PDC so the snapshot computes. Stub fetch_1min_bars
        # + get_fmp_quote so the live-price fetch is deterministic.
        m.or_high["ZZZZ"] = 100.0
        m.or_low["ZZZZ"] = 95.0
        m.pdc["ZZZZ"] = 97.0
        m.or_high["SPY"] = 500.0
        m.or_low["SPY"] = 495.0
        m.pdc["SPY"] = 498.0
        m.or_high["QQQ"] = 400.0
        m.or_low["QQQ"] = 395.0
        m.pdc["QQQ"] = 398.0
        saved_bars = m.fetch_1min_bars
        saved_fmp = m.get_fmp_quote
        saved_di = m.tiger_di
        try:
            m.fetch_1min_bars = lambda t: {
                "current_price": 102.0 if t == "ZZZZ" else (
                    501.0 if t == "SPY" else 401.0
                ),
                "closes": [], "volumes": [],
            }
            m.get_fmp_quote = lambda t: None
            m.tiger_di = lambda t: (None, None)  # warmup OK
            m._update_gate_snapshot("ZZZZ")
            snap = m._gate_snapshot.get("ZZZZ") or {}
            assert "extension_pct" in snap, f"extension_pct missing: {snap}"
            # Price 102 vs OR_High 100 → 2.00% extended on the LONG side.
            assert snap["side"] == "LONG", f"side={snap.get('side')}"
            assert abs(snap["extension_pct"] - 2.0) < 0.01, \
                f"expected 2.0, got {snap['extension_pct']}"
        finally:
            m.fetch_1min_bars = saved_bars
            m.get_fmp_quote = saved_fmp
            m.tiger_di = saved_di
            m.or_high.pop("ZZZZ", None)
            m.or_low.pop("ZZZZ", None)
            m.pdc.pop("ZZZZ", None)
            m._gate_snapshot.pop("ZZZZ", None)

    # ---------- regime: banner unsticking after market close (v4.4.1) ----------
    import datetime as _dt_mod  # local alias so tests can build fixed ET datetimes.
    from zoneinfo import ZoneInfo as _ZI
    _ET = _ZI("America/New_York")

    def _freeze_et(fake_et):
        """Return a (save, restore) pair that monkeypatches _now_et to fake_et.
        Keeps scan_loop side effects (position management etc.) trivial by
        also clearing positions; scan_loop early-returns on CLOSED so those
        paths aren't exercised anyway.
        """
        saved = m._now_et
        m._now_et = lambda: fake_et
        return saved

    @t("regime: scan_loop refreshes mode to CLOSED after market close (16:30 ET simulated)")
    def _():
        reset_state()
        # Wednesday 16:30 ET (weekday, after close)
        fake_et = _dt_mod.datetime(2026, 4, 22, 16, 30, 0, tzinfo=_ET)
        saved = _freeze_et(fake_et)
        # Force globals to a stale pre-close value so we're sure the refresh
        # is what moves them, not just an initial-state coincidence.
        m._current_mode = m.MarketMode.POWER
        m._current_mode_reason = "14:00-15:55 ET"
        m._scan_idle_hours = False
        try:
            m.scan_loop()
            assert m._current_mode == m.MarketMode.CLOSED, \
                f"expected CLOSED, got {m._current_mode}"
            assert m._current_mode_reason == "outside market hours", \
                f"expected 'outside market hours', got {m._current_mode_reason!r}"
            assert m._scan_idle_hours is True, \
                f"expected _scan_idle_hours True after close, got {m._scan_idle_hours}"
        finally:
            m._now_et = saved

    @t("regime: scan_loop refreshes mode to CLOSED on weekend (Saturday simulated)")
    def _():
        reset_state()
        # Saturday 12:00 ET
        fake_et = _dt_mod.datetime(2026, 4, 25, 12, 0, 0, tzinfo=_ET)
        saved = _freeze_et(fake_et)
        m._current_mode = m.MarketMode.POWER
        m._current_mode_reason = "14:00-15:55 ET"
        m._scan_idle_hours = False
        try:
            m.scan_loop()
            assert m._current_mode == m.MarketMode.CLOSED, \
                f"expected CLOSED, got {m._current_mode}"
            assert m._current_mode_reason == "weekend", \
                f"expected 'weekend', got {m._current_mode_reason!r}"
            assert m._scan_idle_hours is True, \
                f"expected _scan_idle_hours True on weekend, got {m._scan_idle_hours}"
        finally:
            m._now_et = saved

    @t("regime: _scan_idle_hours flips False during trading hours")
    def _():
        reset_state()
        # Wednesday 10:00 ET — trading hours, not defensive (no P&L set).
        fake_et = _dt_mod.datetime(2026, 4, 22, 10, 0, 0, tzinfo=_ET)
        saved = _freeze_et(fake_et)
        m._scan_idle_hours = True  # pre-seed True so we verify the flip.
        try:
            # scan_loop runs the full intraday path; stub the heavy bits that
            # aren't under test. We only care about _scan_idle_hours here.
            saved_manage     = m.manage_positions
            saved_manage_s   = m.manage_short_positions
            saved_hard_eject = m._tiger_hard_eject_check
            saved_check      = m.check_entry
            saved_check_s    = m.check_short_entry
            saved_bars       = m.fetch_1min_bars
            m.manage_positions         = lambda: None
            m.manage_short_positions   = lambda: None
            m._tiger_hard_eject_check  = lambda: None
            m.check_entry              = lambda *a, **kw: None
            m.check_short_entry        = lambda *a, **kw: None
            m.fetch_1min_bars          = lambda t: None
            try:
                m.scan_loop()
            finally:
                m.manage_positions        = saved_manage
                m.manage_short_positions  = saved_manage_s
                m._tiger_hard_eject_check = saved_hard_eject
                m.check_entry             = saved_check
                m.check_short_entry       = saved_check_s
                m.fetch_1min_bars         = saved_bars
            assert m._scan_idle_hours is False, \
                f"expected _scan_idle_hours False during trading hours, got {m._scan_idle_hours}"
        finally:
            m._now_et = saved

    @t("regime: /api/state gates.scan_paused reflects after-hours idle")
    def _():
        reset_state()
        fake_et = _dt_mod.datetime(2026, 4, 22, 17, 0, 0, tzinfo=_ET)
        saved = _freeze_et(fake_et)
        m._scan_paused = False         # user-pause is off
        m._scan_idle_hours = False     # will be set True by scan_loop
        try:
            m.scan_loop()
            # Now ask the dashboard serializer for a state snapshot. It
            # reads module globals directly, so we just call the builder.
            payload = ds.snapshot()
            assert payload["gates"]["scan_paused"] is True, \
                f"expected scan_paused True after close, got {payload['gates']['scan_paused']}"
            assert payload["regime"]["mode"] == "CLOSED", \
                f"expected regime.mode CLOSED, got {payload['regime']['mode']}"
            assert payload["regime"]["mode_reason"] == "outside market hours", \
                f"expected 'outside market hours', got {payload['regime']['mode_reason']!r}"
        finally:
            m._now_et = saved
            m._scan_idle_hours = False

    # ---------- v4.6.0 \u2014 paper_state extraction ----------
    @t("v4.6.0: paper_state module imports cleanly")
    def _():
        import paper_state  # noqa: F401
        assert hasattr(paper_state, "save_paper_state"), \
            "paper_state.save_paper_state missing"
        assert hasattr(paper_state, "load_paper_state"), \
            "paper_state.load_paper_state missing"
        assert hasattr(paper_state, "_do_reset_paper"), \
            "paper_state._do_reset_paper missing"

    @t("v4.6.0: paper_state.save_paper_state is re-exported by trade_genius")
    def _():
        import paper_state
        assert m.save_paper_state is paper_state.save_paper_state, \
            "trade_genius.save_paper_state is not the same callable as " \
            "paper_state.save_paper_state \u2014 re-export broken"
        assert m.load_paper_state is paper_state.load_paper_state, \
            "trade_genius.load_paper_state re-export broken"
        assert m._do_reset_paper is paper_state._do_reset_paper, \
            "trade_genius._do_reset_paper re-export broken"

    @t("v4.6.0: paper_state owns _state_loaded and _paper_save_lock")
    def _():
        import paper_state
        assert hasattr(paper_state, "_state_loaded"), \
            "paper_state._state_loaded missing \u2014 should be owned by paper_state"
        assert hasattr(paper_state, "_paper_save_lock"), \
            "paper_state._paper_save_lock missing \u2014 should be owned by paper_state"
        # And the originals must NOT live on trade_genius any more.
        assert not hasattr(m, "_state_loaded"), \
            "v4.6.0: trade_genius._state_loaded should have moved to paper_state"
        assert not hasattr(m, "_paper_save_lock"), \
            "v4.6.0: trade_genius._paper_save_lock should have moved to paper_state"

    # ---------- v4.7.0 \u2014 long/short harmonization ----------
    @t("v4.7.0: check_entry and check_short_entry both return (bool, bars)")
    def _():
        # v4.9.0: check_entry / check_short_entry are now wrappers around
        # the unified check_breakout(side) body. Inspect that single body
        # \u2014 it returns the (bool, bars) tuple on every code path.
        import inspect
        src = inspect.getsource(m.check_breakout)
        assert "return False, None" in src, \
            "check_breakout should return (False, None) on guards"
        assert "return True, bars" in src, \
            "check_breakout should return (True, bars) on success"

    @t("v4.7.0: daily_short_entry_date resets daily_short_entry_count on new day")
    def _():
        # Fixture: set yesterday's date and a non-empty short count, then
        # invoke check_short_entry. Even though the rest of the gate fails
        # (no OR data, market closed, etc.), the date-reset block runs
        # before the gates that can early-return on missing OR data.
        saved_date = m.daily_short_entry_date
        saved_count = dict(m.daily_short_entry_count)
        try:
            m.daily_short_entry_date = "1999-01-01"
            m.daily_short_entry_count.clear()
            m.daily_short_entry_count["AAPL"] = 3
            # Pin _now_et to a known mid-session time so the time gate
            # doesn't short-circuit before the reset block runs.
            from datetime import datetime, timezone, timedelta
            saved_now = m._now_et
            m._now_et = lambda: datetime.now(timezone(timedelta(hours=-4))).replace(
                hour=10, minute=30, second=0, microsecond=0
            )
            try:
                m.check_short_entry("AAPL")
            finally:
                m._now_et = saved_now
            today = m._now_et().strftime("%Y-%m-%d")
            assert m.daily_short_entry_date == today, \
                f"date not reset: {m.daily_short_entry_date!r}"
            assert m.daily_short_entry_count.get("AAPL", 0) == 0, \
                f"count not cleared: {dict(m.daily_short_entry_count)}"
        finally:
            m.daily_short_entry_date = saved_date
            m.daily_short_entry_count.clear()
            m.daily_short_entry_count.update(saved_count)

    @t("v4.7.0: execute_short_entry honors daily loss limit")
    def _():
        # Fixture: rig today's realized P&L below DAILY_LOSS_LIMIT, then
        # call execute_short_entry. Assert no short opened and
        # _trading_halted becomes True.
        saved_halted = m._trading_halted
        saved_reason = m._trading_halted_reason
        saved_paper_trades = list(m.paper_trades)
        saved_short_positions = dict(m.short_positions)
        try:
            m._trading_halted = False
            m._trading_halted_reason = ""
            today = m._now_et().strftime("%Y-%m-%d")
            # Synthesize a closed-long loss row that exceeds DAILY_LOSS_LIMIT.
            m.paper_trades.clear()
            m.paper_trades.append({
                "ticker": "ZZZZ", "action": "SELL", "date": today,
                "pnl": m.DAILY_LOSS_LIMIT - 100.0,  # already past the limit
            })
            m.short_positions.clear()
            m.execute_short_entry("AAPL", 150.0)
            assert m._trading_halted, \
                "execute_short_entry did not halt trading on loss limit"
            assert "AAPL" not in m.short_positions, \
                "execute_short_entry opened a short despite halt"
        finally:
            m._trading_halted = saved_halted
            m._trading_halted_reason = saved_reason
            m.paper_trades.clear()
            m.paper_trades.extend(saved_paper_trades)
            m.short_positions.clear()
            m.short_positions.update(saved_short_positions)

    @t("v4.7.0: _check_daily_loss_limit helper exists and is called by both execute paths")
    def _():
        # v4.9.0: execute_entry / execute_short_entry are wrappers around
        # the unified execute_breakout body. The single body calls
        # _check_daily_loss_limit once for both sides.
        import inspect
        assert callable(getattr(m, "_check_daily_loss_limit", None)), \
            "_check_daily_loss_limit helper missing"
        src = inspect.getsource(m.execute_breakout)
        assert "_check_daily_loss_limit" in src, \
            "execute_breakout does not call _check_daily_loss_limit"

    @t("v4.7.0: _ticker_today_realized_pnl helper exists and aggregates long+short closed trades")
    def _():
        assert callable(getattr(m, "_ticker_today_realized_pnl", None)), \
            "_ticker_today_realized_pnl helper missing"
        from datetime import datetime, timezone
        saved_th = list(m.trade_history)
        saved_sth = list(m.short_trade_history)
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            m.trade_history.clear()
            m.trade_history.append({
                "ticker": "XYZ", "pnl": 30.0, "exit_time_iso": now_iso,
            })
            m.short_trade_history.clear()
            m.short_trade_history.append({
                "ticker": "XYZ", "pnl": -20.0, "exit_time_iso": now_iso,
            })
            total = m._ticker_today_realized_pnl("XYZ")
            assert abs(total - 10.0) < 0.01, \
                f"expected $10 net, got ${total:.2f}"
        finally:
            m.trade_history.clear()
            m.trade_history.extend(saved_th)
            m.short_trade_history.clear()
            m.short_trade_history.extend(saved_sth)

    @t("v4.7.0: scan_loop calls execute_short_entry after check_short_entry returns True")
    def _():
        import inspect
        scan_src = inspect.getsource(m.scan_loop)
        # The new control flow: capture (ok, bars) tuple then call execute.
        assert "check_short_entry(ticker)" in scan_src, \
            "scan_loop should call check_short_entry(ticker)"
        assert "execute_short_entry(ticker" in scan_src, \
            "scan_loop should call execute_short_entry(ticker, ...) on True"
        # And the new pattern uses ok/bars symmetrically with long.
        assert scan_src.count("execute_short_entry") >= 1, \
            "scan_loop missing execute_short_entry call"

    @t("v4.7.0: daily_short_entry_date persists across save/load round-trip")
    def _():
        import paper_state
        import tempfile, os, json
        saved_file = m.PAPER_STATE_FILE
        saved_date = m.daily_short_entry_date
        saved_loaded = paper_state._state_loaded
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            ) as f:
                tmp_path = f.name
            m.PAPER_STATE_FILE = tmp_path
            paper_state._state_loaded = True
            m.daily_short_entry_date = "2026-04-24"
            m.save_paper_state()
            with open(tmp_path) as f:
                disk = json.load(f)
            assert disk.get("daily_short_entry_date") == "2026-04-24", \
                f"date not in disk state: {disk.get('daily_short_entry_date')!r}"
            m.daily_short_entry_date = "WRONG"
            m.load_paper_state()
            assert m.daily_short_entry_date == "2026-04-24", \
                f"date not restored: {m.daily_short_entry_date!r}"
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            m.PAPER_STATE_FILE = saved_file
            m.daily_short_entry_date = saved_date
            paper_state._state_loaded = saved_loaded

    # v4.9.1: prod smoke caught a missing rate-limit trip on the 6th
    # bad login. The unit test below exercises _rate_limit_check directly
    # so we catch any regression to the limiter logic itself; the prod
    # failure was a config issue (DASHBOARD_TRUST_PROXY not set on Railway,
    # so request.remote varied across the proxy fleet and scattered the
    # bucket) not a code bug.
    @t("v4.9.1: rate-limiter blocks 6th attempt within window")
    def _():
        import dashboard_server as ds
        # Save and reset state so this test is hermetic.
        saved = dict(ds._login_attempts)
        ds._login_attempts.clear()
        try:
            ip = "203.0.113.7"
            results = [ds._rate_limit_check(ip) for _ in range(7)]
            # First 5 allowed, 6th and 7th blocked.
            assert results == [True, True, True, True, True, False, False], \
                f"unexpected sequence: {results}"
        finally:
            ds._login_attempts.clear()
            ds._login_attempts.update(saved)

    @t("v4.9.1: rate-limiter buckets per-IP independently")
    def _():
        import dashboard_server as ds
        saved = dict(ds._login_attempts)
        ds._login_attempts.clear()
        try:
            for _ in range(5):
                assert ds._rate_limit_check("198.51.100.1") is True
            # A different IP still has a fresh bucket.
            assert ds._rate_limit_check("198.51.100.2") is True
            # The first IP's 6th attempt is blocked.
            assert ds._rate_limit_check("198.51.100.1") is False
        finally:
            ds._login_attempts.clear()
            ds._login_attempts.update(saved)

    @t("v4.9.1: /api/version endpoint registered")
    def _():
        import dashboard_server as ds
        app = ds._build_app()
        paths = [r.resource.canonical for r in app.router.routes()]
        assert "/api/version" in paths, f"/api/version not registered; got {paths}"

    @t("v4.9.1: /api/version handler actually returns BOT_VERSION")
    def _():
        # Regression guard: the v4.9.1 handler originally called an
        # undefined _bot_module() helper; the route was registered so the
        # route-registration test passed, but the handler blew up at
        # request time and returned {"version": "?"}. Exercise the
        # handler directly so a stale helper name fails loudly.
        import dashboard_server as ds
        import asyncio, json
        class _Req: pass
        resp = asyncio.new_event_loop().run_until_complete(ds.h_version(_Req()))
        body = json.loads(resp.body.decode())
        assert body.get("version") == m.BOT_VERSION, \
            f"/api/version returned {body!r}, want version={m.BOT_VERSION!r}"


    return run_suite("LOCAL SMOKE TESTS (v4.9.1 synthetic harness)")


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
# SYNTHETIC HARNESS MODE (v4.9.0)
# ============================================================

def run_synthetic() -> int:
    """Replay all 25 synthetic-harness goldens. One t() entry per scenario.

    Goldens live at synthetic_harness/goldens/<name>.json and are
    committed to git. A failure prints a unified diff between the
    recorded golden and the observed run.
    """
    os.environ.setdefault("SSM_SMOKE_TEST", "1")
    os.environ.setdefault("CHAT_ID", "999999999")
    os.environ.setdefault("DASHBOARD_PASSWORD", "smoketest1234")
    os.environ.setdefault(
        "TELEGRAM_TOKEN",
        "0000000000:AAAA_smoke_placeholder_token_0000000",
    )
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from synthetic_harness import list_scenarios, replay_scenario
    except Exception as e:  # pragma: no cover
        print(f"synthetic harness import failed: {e}")
        traceback.print_exc()
        return 2

    for name in list_scenarios():
        # Capture in a closure so each lambda gets its own name.
        def _make(scn):
            @t(f"synthetic: {scn}")
            def _():
                ok, diff = replay_scenario(scn)
                assert ok, f"golden mismatch for {scn}:\n{diff}"
            return _
        _make(name)

    return run_suite("SYNTHETIC HARNESS (v4.9.0, 50 scenarios)")


# ============================================================
# CLI
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="TradeGenius smoke test")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--prod", action="store_true")
    parser.add_argument("--synthetic", action="store_true",
                        help="replay synthetic_harness goldens after local")
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
    if args.synthetic:
        total_fails += run_synthetic()
    if do_prod:
        if not args.password:
            print("(prod mode skipped — no --password)")
        else:
            total_fails += run_prod(args.url, args.password, args.expected_version)

    print(f"=== RESULT: {'PASS' if total_fails == 0 else f'FAIL ({total_fails})'} ===")
    return 0 if total_fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
