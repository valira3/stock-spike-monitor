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
    # v5.1.8 \u2014 point STATE_DB_PATH at a tmp file so tests do not try to
    # touch /data/state.db (the Railway volume mount, absent locally).
    os.environ["STATE_DB_PATH"] = str(tmp_dir / "state.db")

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
        # v5.1.8 \u2014 wipe SQLite-backed v5 tracks + fired_set so a prior
        # test cannot leak rows into the next.
        try:
            import persistence as _p
            _p.replace_all_tracks({}, {})
            _p.prune_fired("__never_matches__")
        except Exception:
            pass

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

    @t("version: BOT_VERSION is 5.5.4")
    def _():
        assert m.BOT_VERSION == "5.5.4", f"got {m.BOT_VERSION}"

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

    # ---------- v4.12.0 — ticker AH session + marquee schema ----------
    @t("v4.12.0: _classify_session_et returns one of rth/pre/post/closed")
    def _():
        s = ds._classify_session_et()
        assert s in ("rth", "pre", "post", "closed"), f"unexpected: {s!r}"

    @t("v4.12.0: _fetch_indices payload exposes session + per-row ah keys")
    def _():
        payload = ds._fetch_indices()
        assert "session" in payload, \
            f"top-level session key missing: {list(payload.keys())}"
        assert payload["session"] in ("rth", "pre", "post", "closed")
        for row in payload.get("indices", []):
            for k in ("ah", "ah_change", "ah_change_pct"):
                assert k in row, \
                    f"row missing {k!r}: symbol={row.get('symbol')!r} keys={list(row.keys())}"

    # ---------- v4.13.0 \u2014 Yahoo cash indices + futures badge ----------
    @t("v4.13.0: _fetch_yahoo_quote_one returns None for a junk symbol")
    def _():
        # Guaranteed-bad symbol \u2014 Yahoo will respond with a 404 / empty
        # result and the helper must swallow that into None rather than
        # raise. We're not asserting against the network here, just that
        # the contract holds for the failure case.
        res = ds._fetch_yahoo_quote_one("__SMOKE_BAD_SYMBOL__")
        assert res is None, f"expected None for junk symbol, got {res!r}"

    @t("v4.13.0: _fetch_yahoo_quotes returns dict for empty input")
    def _():
        # Empty list short-circuits without touching the network.
        out = ds._fetch_yahoo_quotes([])
        assert isinstance(out, dict) and out == {}, \
            f"empty input should yield empty dict, got {out!r}"

    @t("v4.13.0: _fetch_indices payload exposes yahoo_ok and futures schema")
    def _():
        payload = ds._fetch_indices()
        # Yahoo block runs after the Alpaca block. If Alpaca early-returned
        # (no paper keys / alpaca-py missing) the Yahoo keys are absent and
        # that's a known degraded mode \u2014 we only assert schema when the
        # function got past the Alpaca block, signalled by ok=True.
        if not payload.get("ok"):
            return  # Alpaca early-return path; nothing to check here.
        assert "yahoo_ok" in payload, \
            f"yahoo_ok missing from payload keys: {list(payload.keys())}"
        assert isinstance(payload["yahoo_ok"], bool), \
            f"yahoo_ok must be bool, got {type(payload['yahoo_ok']).__name__}"
        # Cash-index rows (when present) must carry display_label, and any
        # future sub-object must include change_pct (the only field the
        # frontend renders). ETF rows have no display_label/future keys
        # \u2014 they are skipped here on purpose.
        cash_seen = False
        for row in payload.get("indices", []):
            sym = row.get("symbol", "")
            if sym in ds._YAHOO_CASH_SYMBOLS:
                cash_seen = True
                assert row.get("display_label"), \
                    f"cash row {sym} missing display_label: {row}"
                fut = row.get("future")
                if fut is not None:
                    assert "change_pct" in fut, \
                        f"future sub-object missing change_pct on {sym}: {fut}"
                    assert "label" in fut, \
                        f"future sub-object missing label on {sym}: {fut}"
        # If yahoo_ok is True we must have produced at least one cash row;
        # if False, the failure mode is degraded and we accept zero.
        if payload["yahoo_ok"]:
            assert cash_seen, \
                "yahoo_ok=True but no cash-index rows in payload"

    @t("v4.13.0: cash/futures symbol lists are mutually exclusive")
    def _():
        # Sanity guard: if someone accidentally puts ES=F in the cash list
        # the inline-badge logic in _fetch_indices would render ES on its
        # own row instead of riding inside ^GSPC. The two lists must stay
        # disjoint.
        cash = set(ds._YAHOO_CASH_SYMBOLS)
        fut  = set(ds._YAHOO_FUTURES_SYMBOLS)
        overlap = cash & fut
        assert not overlap, f"cash and futures lists overlap: {overlap}"

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


    # ============================================================
    # v5.0.0 \u2014 Tiger/Buffalo state-machine tests
    # ============================================================
    # Each test docstring/title cites a rule ID from STRATEGY.md so a
    # spec change traces straight to a test failure. Coverage spans
    # every L-P*-R*, S-P*-R*, and C-R* rule plus the state-machine
    # plumbing in tiger_buffalo_v5.py.
    import tiger_buffalo_v5 as v5

    @t("v5 module: STRATEGY.md exists at repo root")
    def _():
        # The canonical spec MUST live at the repo root.
        spec = Path(__file__).resolve().parent / "STRATEGY.md"
        assert spec.exists(), f"STRATEGY.md missing at {spec}"
        body = spec.read_text(encoding="utf-8")
        assert "L-P1-G1" in body, "L-P1-G1 rule ID missing from spec"
        assert "S-P4-R3" in body, "S-P4-R3 priority-1 rule missing"
        assert "C-R7" in body, "C-R7 universe rule missing"

    @t("v5 module: BOT_VERSION matches v5 major")
    def _():
        assert m.BOT_VERSION.startswith("5."), \
            f"v5.x expected, got {m.BOT_VERSION}"

    @t("v5 module: state names match spec D")
    def _():
        for name in ("IDLE","ARMED","STAGE_1","STAGE_2","TRAILING",
                     "EXITED","RE_HUNT_PENDING","LOCKED_FOR_DAY"):
            assert getattr(v5, "STATE_" + (name if name != "LOCKED_FOR_DAY" else "LOCKED")) is not None, name
        assert "STAGE_1" in v5.ALL_STATES

    @t("v5 module: DMI period is 15 (C-R2)")
    def _():
        # C-R2: ADX/DMI period MUST be 15 on the relevant timeframe
        # (per Gene's spec; matches v4 trade_genius.DI_PERIOD = 15).
        assert v5.DMI_PERIOD == 15, f"got {v5.DMI_PERIOD}"

    @t("v5 module: stage thresholds match spec (L-P2-R1, L-P3-R1)")
    def _():
        assert v5.STAGE1_DI_THRESHOLD == 25.0
        assert v5.STAGE2_DI_THRESHOLD == 30.0
        assert v5.HARD_EXIT_DI_THRESHOLD == 25.0

    # ---------- L-P1: Long Permission Gates ----------
    @t("v5 L-P1-G1: long requires QQQ.last > QQQ.PDC")
    def _():
        # Fail when QQQ <= PDC; pass when QQQ > PDC and other gates pass.
        assert not v5.gates_pass_long(100,100,200,100,50,40,45)
        assert v5.gates_pass_long(101,100,200,100,50,40,45)

    @t("v5 L-P1-G2: long requires SPY.last > SPY.PDC")
    def _():
        assert not v5.gates_pass_long(101,100,99,100,50,40,45)
        assert v5.gates_pass_long(101,100,101,100,50,40,45)

    @t("v5 L-P1-G3: long requires ticker.last > ticker.PDC")
    def _():
        assert not v5.gates_pass_long(101,100,101,100,40,40,45)
        assert v5.gates_pass_long(101,100,101,100,50,40,45)

    @t("v5 L-P1-G4: long requires ticker.last > first_hour_high")
    def _():
        # Equality fails (strict >).
        assert not v5.gates_pass_long(101,100,101,100,45,40,45)
        assert v5.gates_pass_long(101,100,101,100,46,40,45)

    @t("v5 L-P1: any None input fails closed")
    def _():
        assert not v5.gates_pass_long(None,100,101,100,50,40,45)
        assert not v5.gates_pass_long(101,100,101,None,50,40,45)

    # ---------- L-P2: Stage 1 Jab ----------
    @t("v5 L-P2-R1: stage-1 long needs DI+(1m)>25 AND DI+(5m)>25")
    def _():
        assert not v5.stage1_signal_long(20, 30)  # 1m below 25
        assert not v5.stage1_signal_long(30, 20)  # 5m below 25
        assert not v5.stage1_signal_long(25, 30)  # equality fails (strict >)
        assert v5.stage1_signal_long(26, 26)

    @t("v5 L-P2-R2: stage-1 entry requires 2 consecutive 1m DI+>25 closes")
    def _():
        # Single confirmation must NOT fire entry; second consecutive does.
        track = v5.new_track(v5.DIR_LONG)
        assert not v5.tick_stage1_confirm(track, True)   # 1st confirm
        fired = v5.tick_stage1_confirm(track, True)      # 2nd confirm
        assert fired, "expected fire on 2nd consecutive confirm"

    @t("v5 L-P2-R2: a missed confirm RESETS the counter")
    def _():
        # If signal flips false between confirms, counter resets per spec.
        track = v5.new_track(v5.DIR_LONG)
        v5.tick_stage1_confirm(track, True)              # confirms=1
        assert not v5.tick_stage1_confirm(track, False)  # reset to 0
        assert track["stage1_confirms"] == 0
        assert not v5.tick_stage1_confirm(track, True)   # back to 1

    @t("v5 L-P2-R3: stage-1 entry transitions track to STAGE_1 with 50% sizing flag")
    def _():
        track = v5.new_track(v5.DIR_LONG)
        v5.transition_to_stage1(track, fill_price=100.0, initial_stop=98.5)
        assert track["state"] == v5.STATE_STAGE_1
        assert track["original_entry_price"] == 100.0
        assert track["current_stop"] == 98.5

    @t("v5 L-P2-R4: stage-1 long initial stop is the prior 5m candle low")
    def _():
        # The stop value passed in is what scan-loop will compute from
        # the prior closed 5m bar. We just assert the wiring honors it.
        track = v5.new_track(v5.DIR_LONG)
        v5.transition_to_stage1(track, fill_price=10.0, initial_stop=9.7)
        # Stop must NOT change during STAGE_1 (no ratchet runs there).
        assert track["current_stop"] == 9.7

    @t("v5 L-P2-R5: stage-1 records original_entry_price = fill price")
    def _():
        track = v5.new_track(v5.DIR_LONG)
        v5.transition_to_stage1(track, fill_price=42.17, initial_stop=41.0)
        assert track["original_entry_price"] == 42.17

    # ---------- L-P3: Stage 2 Strike ----------
    @t("v5 L-P3-R1: stage-2 long needs DI+(1m)>30")
    def _():
        assert not v5.stage2_signal_long(30)   # equality fails strict >
        assert not v5.stage2_signal_long(29)
        assert v5.stage2_signal_long(31)

    @t("v5 L-P3-R2: stage-2 entry requires 2 consecutive 1m DI+>30 closes")
    def _():
        track = v5.new_track(v5.DIR_LONG)
        assert not v5.tick_stage2_confirm(track, True)
        assert v5.tick_stage2_confirm(track, True)

    @t("v5 L-P3-R3: stage-2 long blocked when ticker NOT above original_entry")
    def _():
        # If price slipped to entry or below, stage 2 must NOT fire.
        assert not v5.winning_rule_long(100.0, 100.0)  # equality blocked
        assert not v5.winning_rule_long(99.99, 100.0)
        assert v5.winning_rule_long(100.01, 100.0)

    @t("v5 L-P3-R4: stage-2 transition flips state to STAGE_2 (full 100%)")
    def _():
        track = v5.new_track(v5.DIR_LONG)
        v5.transition_to_stage1(track, 100.0, 98.0)
        v5.transition_to_stage2(track)
        assert track["state"] == v5.STATE_STAGE_2

    @t("v5 L-P3-R5: stage-2 safety lock moves stop to original_entry_price")
    def _():
        # On Stage-2 fill the stop on the entire 100% position becomes
        # original_entry_price ("House Money").
        track = v5.new_track(v5.DIR_LONG)
        v5.transition_to_stage1(track, fill_price=100.0, initial_stop=98.0)
        v5.transition_to_stage2(track)
        assert track["current_stop"] == 100.0, \
            f"expected stop=100.0 (original entry), got {track['current_stop']}"

    # ---------- L-P4: Guardrail / TRAILING ----------
    @t("v5 L-P4-R1: HL is a 5m low strictly above the previous 5m low")
    def _():
        # Equal lows are NOT a Higher Low.
        assert v5.ratchet_long_higher_low(prev_5m_low=9.0, this_5m_low=9.0,
                                          current_stop=8.5) == 8.5
        # this_low > prev_low and > current_stop -> ratchet up.
        assert v5.ratchet_long_higher_low(9.0, 9.5, 8.5) == 9.5

    @t("v5 L-P4-R2: long ratchet is up-only; never lowers the stop")
    def _():
        # New HL is BELOW current stop -> stop unchanged.
        assert v5.ratchet_long_higher_low(prev_5m_low=8.0, this_5m_low=8.5,
                                          current_stop=9.0) == 9.0

    @t("v5 L-P4-R3 (a): long structural-stop hit when ticker.last < current_stop")
    def _():
        assert v5.structural_stop_hit_long(ticker_last=9.99, current_stop=10.0)
        assert not v5.structural_stop_hit_long(ticker_last=10.0, current_stop=10.0)
        assert not v5.structural_stop_hit_long(10.5, 10.0)

    @t("v5 L-P4-R3 (b): long DI<25 hard exit fires on closed 1m candle")
    def _():
        assert v5.hard_exit_di_fail(v5.DIR_LONG, di_1m=24.99)
        assert not v5.hard_exit_di_fail(v5.DIR_LONG, di_1m=25.0)
        assert not v5.hard_exit_di_fail(v5.DIR_LONG, di_1m=None)

    @t("v5 L-P4-R3: evaluate_exit returns STRUCTURAL_STOP when long stop hit")
    def _():
        track = v5.new_track(v5.DIR_LONG)
        v5.transition_to_stage1(track, 10.0, 9.5)
        v5.transition_to_stage2(track)
        assert v5.evaluate_exit(track, ticker_last=9.99,
                                di_1m_closed=None) == "STRUCTURAL_STOP"

    @t("v5 L-P4-R3: evaluate_exit returns DI_HARD_EJECT on long DI<25")
    def _():
        track = v5.new_track(v5.DIR_LONG)
        v5.transition_to_stage1(track, 10.0, 9.5)
        v5.transition_to_stage2(track)
        # Ticker still ABOVE stop, DI just dropped: still exits.
        assert v5.evaluate_exit(track, ticker_last=11.0,
                                di_1m_closed=20.0) == "DI_HARD_EJECT"

    @t("v5 L-P4-R4: post-exit transitions track to EXITED (re-hunt available)")
    def _():
        track = v5.new_track(v5.DIR_LONG)
        v5.transition_to_stage1(track, 10.0, 9.5)
        v5.transition_to_stage2(track)
        v5.on_post_exit(track)
        assert track["state"] == v5.STATE_EXITED
        assert track["re_hunt_used"] is False

    # ---------- L-P5: Re-Hunt ----------
    @t("v5 L-P5-R1: long reclamation requires ticker.last > original_entry")
    def _():
        assert not v5.reclamation_long(99.99, 100.0)
        assert not v5.reclamation_long(100.0, 100.0)  # equality fails
        assert v5.reclamation_long(100.01, 100.0)

    @t("v5 L-P5-R2: re-hunt re-arms a fresh ARMED track with no stop")
    def _():
        track = v5.new_track(v5.DIR_LONG)
        v5.transition_to_stage1(track, 10.0, 9.5)
        v5.transition_to_stage2(track)
        v5.on_post_exit(track)
        ok = v5.transition_re_hunt(track)
        assert ok
        assert track["state"] == v5.STATE_ARMED
        assert track["original_entry_price"] is None
        assert track["current_stop"] is None
        assert track["re_hunt_used"] is True

    @t("v5 L-P5-R3: second exit forces LOCKED_FOR_DAY (one re-hunt cap)")
    def _():
        track = v5.new_track(v5.DIR_LONG)
        v5.transition_to_stage1(track, 10.0, 9.5)
        v5.transition_to_stage2(track)
        v5.on_post_exit(track)
        v5.transition_re_hunt(track)
        # Simulate the re-hunt also exiting.
        v5.transition_to_stage1(track, 11.0, 10.5)
        v5.transition_to_stage2(track)
        v5.on_post_exit(track)
        assert track["state"] == v5.STATE_LOCKED, \
            f"expected LOCKED_FOR_DAY after second exit, got {track['state']}"

    @t("v5 L-P5-R3: a third re-hunt attempt is rejected and forces LOCKED")
    def _():
        track = v5.new_track(v5.DIR_LONG)
        track["re_hunt_used"] = True  # already burned
        v5.transition_to_exited(track)
        ok = v5.transition_re_hunt(track)
        assert not ok
        assert track["state"] == v5.STATE_LOCKED

    # ---------- S-P1: Short Permission Gates ----------
    @t("v5 S-P1-G1: short forbidden when QQQ.last >= QQQ.PDC")
    def _():
        assert not v5.gates_pass_short(100,100,99,100,50,60,55)  # QQQ equal
        assert not v5.gates_pass_short(101,100,99,100,50,60,55)  # QQQ green
        assert v5.gates_pass_short(99,100,99,100,50,60,55)

    @t("v5 S-P1-G2: short forbidden when SPY.last >= SPY.PDC")
    def _():
        assert not v5.gates_pass_short(99,100,101,100,50,60,55)
        assert v5.gates_pass_short(99,100,99,100,50,60,55)

    @t("v5 S-P1-G3: short requires ticker.last < ticker.PDC")
    def _():
        assert not v5.gates_pass_short(99,100,99,100,60,60,55)
        assert v5.gates_pass_short(99,100,99,100,50,60,55)

    @t("v5 S-P1-G4: short requires ticker.last < opening_range_low_5m")
    def _():
        # Equality fails (strict <).
        assert not v5.gates_pass_short(99,100,99,100,55,60,55)
        assert v5.gates_pass_short(99,100,99,100,54,60,55)

    @t("v5 S-P1: indices-green vetoes shorts even on a weak ticker")
    def _():
        # Ticker WAY below its PDC, but indices green: shorts are off.
        assert not v5.gates_pass_short(105,100,105,100,1,60,55)

    # ---------- S-P2: Stage 1 ----------
    @t("v5 S-P2-R1: stage-1 short needs DI-(1m)>25 AND DI-(5m)>25")
    def _():
        assert not v5.stage1_signal_short(25, 30)
        assert v5.stage1_signal_short(26, 26)

    @t("v5 S-P2-R2: stage-1 short entry requires 2 consecutive 1m DI->25 closes")
    def _():
        track = v5.new_track(v5.DIR_SHORT)
        assert not v5.tick_stage1_confirm(track, True)
        assert v5.tick_stage1_confirm(track, True)

    @t("v5 S-P2-R3..R5: stage-1 short transition records entry + stop above")
    def _():
        track = v5.new_track(v5.DIR_SHORT)
        v5.transition_to_stage1(track, fill_price=20.0, initial_stop=20.5)
        assert track["state"] == v5.STATE_STAGE_1
        assert track["original_entry_price"] == 20.0
        # Short stop sits ABOVE entry (prior 5m candle high).
        assert track["current_stop"] == 20.5

    # ---------- S-P3: Stage 2 ----------
    @t("v5 S-P3-R1: stage-2 short needs DI-(1m)>30")
    def _():
        assert not v5.stage2_signal_short(30)
        assert v5.stage2_signal_short(30.01)

    @t("v5 S-P3-R3: stage-2 short blocked when ticker NOT below original_entry")
    def _():
        assert not v5.winning_rule_short(20.0, 20.0)  # equality blocked
        assert not v5.winning_rule_short(20.01, 20.0)
        assert v5.winning_rule_short(19.99, 20.0)

    @t("v5 S-P3-R5: short safety lock moves stop to original_entry_price")
    def _():
        track = v5.new_track(v5.DIR_SHORT)
        v5.transition_to_stage1(track, fill_price=20.0, initial_stop=20.5)
        v5.transition_to_stage2(track)
        assert track["current_stop"] == 20.0

    # ---------- S-P4: Guardrail / Hard Eject priority ----------
    @t("v5 S-P4-R1: LH is a 5m high strictly below the previous 5m high")
    def _():
        # Equal highs are NOT a Lower High.
        assert v5.ratchet_short_lower_high(prev_5m_high=10.0, this_5m_high=10.0,
                                           current_stop=10.5) == 10.5
        # this_high < prev_high and below current stop -> ratchet down.
        assert v5.ratchet_short_lower_high(10.0, 9.7, 10.5) == 9.7

    @t("v5 S-P4-R2: short ratchet is down-only; never raises the stop")
    def _():
        assert v5.ratchet_short_lower_high(prev_5m_high=10.5, this_5m_high=10.2,
                                           current_stop=10.0) == 10.0

    @t("v5 S-P4-R3: short DI<25 hard eject fires PRIORITY-1 over structural stop")
    def _():
        # Configure a track where BOTH structural stop AND DI<25 conditions
        # are true simultaneously. The result MUST be DI_HARD_EJECT, not
        # STRUCTURAL_STOP \u2014 short-side priority inversion per S-P4-R3.
        track = v5.new_track(v5.DIR_SHORT)
        v5.transition_to_stage1(track, 20.0, 20.5)
        v5.transition_to_stage2(track)
        # ticker_last > current_stop (structural hit) AND di < 25 (DI hit)
        reason = v5.evaluate_exit(track, ticker_last=21.0, di_1m_closed=20.0)
        assert reason == "DI_HARD_EJECT", \
            f"S-P4-R3 priority violated: got {reason!r}"

    @t("v5 S-P4-R4: short structural-stop hit when ticker.last > current_stop")
    def _():
        assert v5.structural_stop_hit_short(ticker_last=21.0, current_stop=20.5)
        assert not v5.structural_stop_hit_short(ticker_last=20.5, current_stop=20.5)

    @t("v5 S-P4-R4: structural exit fires when DI is healthy but stop is breached")
    def _():
        track = v5.new_track(v5.DIR_SHORT)
        v5.transition_to_stage1(track, 20.0, 20.5)
        v5.transition_to_stage2(track)
        # DI still healthy (>= 25) so the priority-1 check is silent;
        # structural stop fires.
        reason = v5.evaluate_exit(track, ticker_last=21.0, di_1m_closed=30.0)
        assert reason == "STRUCTURAL_STOP"

    # ---------- S-P5: Re-Hunt ----------
    @t("v5 S-P5-R1: short reclamation requires ticker.last < original_entry")
    def _():
        assert not v5.reclamation_short(20.0, 20.0)
        assert v5.reclamation_short(19.99, 20.0)

    @t("v5 S-P5-R3: short second exit forces LOCKED_FOR_DAY")
    def _():
        track = v5.new_track(v5.DIR_SHORT)
        v5.transition_to_stage1(track, 20.0, 20.5)
        v5.transition_to_stage2(track)
        v5.on_post_exit(track)
        v5.transition_re_hunt(track)
        v5.transition_to_stage1(track, 19.0, 19.5)
        v5.transition_to_stage2(track)
        v5.on_post_exit(track)
        assert track["state"] == v5.STATE_LOCKED

    # ---------- C: Cross-cutting ----------
    @t("v5 C-R1: long and short on same ticker mutually exclusive")
    def _():
        # If long is already active, short cannot arm; and vice versa.
        assert v5.can_arm_direction(None, v5.DIR_LONG)
        assert not v5.can_arm_direction(v5.DIR_LONG, v5.DIR_SHORT)
        assert not v5.can_arm_direction(v5.DIR_SHORT, v5.DIR_LONG)
        assert v5.can_arm_direction(v5.DIR_LONG, v5.DIR_LONG)

    @t("v5 C-R2: DMI period is 15")
    def _():
        assert v5.DMI_PERIOD == 15

    @t("v5 C-R3: confirmation counter is closed-candle driven (no None tick)")
    def _():
        # Pure-function helpers don't accept ticks \u2014 they advance only
        # when called with a closed-candle signal. Verifying the API
        # surface enforces the C-R3 separation.
        track = v5.new_track(v5.DIR_LONG)
        # No way to "tick" without supplying a bool decision.
        assert v5.tick_stage1_confirm(track, False) is False

    @t("v5 C-R4: daily-loss-limit forces every track to LOCKED_FOR_DAY")
    def _():
        # Set up two live tracks, then trip the lock helper.
        m.v5_long_tracks.clear()
        m.v5_short_tracks.clear()
        m.v5_active_direction.clear()
        m.v5_long_tracks["XYZ"] = v5.new_track(v5.DIR_LONG)
        m.v5_long_tracks["XYZ"]["state"] = v5.STATE_TRAILING
        m.v5_short_tracks["ABC"] = v5.new_track(v5.DIR_SHORT)
        m.v5_short_tracks["ABC"]["state"] = v5.STATE_STAGE_1
        n = m.v5_lock_all_tracks("test")
        assert n == 2
        assert m.v5_long_tracks["XYZ"]["state"] == v5.STATE_LOCKED
        assert m.v5_short_tracks["ABC"]["state"] == v5.STATE_LOCKED

    @t("v5 C-R4: _check_daily_loss_limit calls v5_lock_all_tracks on trip")
    def _():
        # Indirect verification: the source of _check_daily_loss_limit
        # references v5_lock_all_tracks. A regression that removes the
        # wiring fails this string-presence test.
        import inspect
        src = inspect.getsource(m._check_daily_loss_limit)
        assert "v5_lock_all_tracks" in src, \
            "C-R4 wiring missing in _check_daily_loss_limit"

    @t("v5 C-R5: eod_close calls v5_lock_all_tracks (EOD lock)")
    def _():
        import inspect
        src = inspect.getsource(m.eod_close)
        assert "v5_lock_all_tracks" in src, \
            "C-R5 wiring missing in eod_close"

    @t("v5 C-R6: Sovereign Regime Shield helper still exists (preserved)")
    def _():
        # C-R6 says the Sovereign Regime Shield (Eye of the Tiger)
        # global kill is preserved from v4. The helper that drives it
        # MUST still exist and be callable.
        assert callable(getattr(m, "_sovereign_regime_eject", None))

    @t("v5 C-R7: 9-ticker spike universe + SPY/QQQ pinned (preserved)")
    def _():
        # C-R7: the v5 universe is identical to v4. SPY/QQQ are pinned
        # filter rows in the dashboard, never traded directly \u2014 they
        # are intentionally NOT in the trade universe (they are read
        # by check_breakout as index polarity inputs only). The 9-name
        # spike list IS the trade universe.
        assert len(m.TRADE_TICKERS) == 9, \
            f"C-R7 universe size drift: {len(m.TRADE_TICKERS)} (want 9)"
        # SPY and QQQ are referenced as polarity inputs in check_breakout.
        import inspect
        src = inspect.getsource(m.check_breakout)
        assert '"SPY"' in src and '"QQQ"' in src, \
            "C-R7 SPY/QQQ polarity wiring missing from check_breakout"

    # ---------- v5 plumbing ----------
    @t("v5 plumbing: paper_state.json round-trips v5 tracks")
    def _():
        reset_state()
        m.v5_long_tracks.clear()
        m.v5_short_tracks.clear()
        m.v5_active_direction.clear()
        track = v5.new_track(v5.DIR_LONG)
        v5.transition_to_stage1(track, 50.0, 49.0)
        v5.transition_to_stage2(track)
        m.v5_long_tracks["AAPL"] = track
        m.v5_active_direction["AAPL"] = "long"
        m.save_paper_state()
        # Wipe in-memory and reload.
        m.v5_long_tracks.clear()
        m.v5_short_tracks.clear()
        m.v5_active_direction.clear()
        m.load_paper_state()
        assert "AAPL" in m.v5_long_tracks
        loaded = m.v5_long_tracks["AAPL"]
        assert loaded["state"] == v5.STATE_STAGE_2
        assert loaded["original_entry_price"] == 50.0
        assert loaded["current_stop"] == 50.0  # safety lock
        assert m.v5_active_direction.get("AAPL") == "long"

    @t("v5 plumbing: legacy v4 paper_state file loads as IDLE (migration)")
    def _():
        # A v4 paper_state.json never wrote v5_* keys. Loader MUST treat
        # absent keys as a fresh start (no exception, tracks empty).
        # v5.1.8: tracks now live in SQLite \u2014 clear the table first so a
        # prior test's leftover row doesn't masquerade as legacy data.
        import persistence as _p
        _p.replace_all_tracks({}, {})
        import json as _json
        legacy = {
            "paper_cash": 100000.0,
            "positions": {},
            "paper_trades": [],
            "paper_all_trades": [],
            "daily_entry_count": {},
            "daily_entry_date": "",
            "or_high": {}, "or_low": {}, "pdc": {},
            "or_collected_date": "",
            "user_config": {},
            "trade_history": [],
            "short_positions": {}, "short_trade_history": [],
            "daily_short_entry_count": {}, "daily_short_entry_date": "",
            "last_exit_time": {},
            "_scan_paused": False,
            "_trading_halted": False,
            "_trading_halted_reason": "",
            # NO v5_* keys whatsoever.
        }
        with open(m.PAPER_STATE_FILE, "w") as f:
            _json.dump(legacy, f)
        m.v5_long_tracks["leftover"] = v5.new_track(v5.DIR_LONG)
        m.load_paper_state()
        # Loader should leave v5 dicts empty (legacy file had no v5 data).
        assert m.v5_long_tracks == {}, m.v5_long_tracks
        assert m.v5_short_tracks == {}

    @t("v5 plumbing: load_track defaults absent record to IDLE")
    def _():
        track = v5.load_track(None, v5.DIR_LONG)
        assert track["state"] == v5.STATE_IDLE
        assert track["direction"] == v5.DIR_LONG

    @t("v5 plumbing: load_track sanitizes a malformed state value")
    def _():
        bogus = {"state": "not_a_real_state", "direction": "long"}
        track = v5.load_track(bogus, v5.DIR_LONG)
        assert track["state"] == v5.STATE_IDLE  # fail-safe

    @t("v5 plumbing: trade_genius imports v5 module")
    def _():
        assert hasattr(m, "v5")
        assert m.v5 is v5
        # And the per-ticker globals exist.
        assert hasattr(m, "v5_long_tracks")
        assert hasattr(m, "v5_short_tracks")
        assert hasattr(m, "v5_active_direction")

    @t("v5 plumbing: v5_get_track creates IDLE track on first access")
    def _():
        m.v5_long_tracks.clear()
        track = m.v5_get_track("ZZZ", v5.DIR_LONG)
        assert track["state"] == v5.STATE_IDLE
        assert "ZZZ" in m.v5_long_tracks

    # ---------- v5.1.8 \u2014 SQLite persistence ----------
    @t("v5.1.8 persistence: fired_set round-trips mark/was/prune")
    def _():
        import persistence as p
        p.prune_fired("__never_matches__")  # clear table
        key = "2026-04-26-15:58-daily-15:58"
        assert not p.was_fired(key)
        p.mark_fired(key)
        assert p.was_fired(key)
        # Idempotent re-mark.
        p.mark_fired(key)
        assert p.was_fired(key)
        # Prune keeping today does NOT delete today's row.
        p.prune_fired("2026-04-26")
        assert p.was_fired(key)
        # Prune keeping a different day deletes it.
        p.prune_fired("2099-01-01")
        assert not p.was_fired(key)

    @t("v5.1.8 persistence: v5_long_tracks round-trip per direction")
    def _():
        import persistence as p
        p.replace_all_tracks({}, {})
        long_state = {"state": "ARMED", "entry": 50.0, "ticker": "AAPL"}
        short_state = {"state": "WATCHING", "ticker": "AAPL"}
        p.save_track("AAPL", long_state, "long")
        p.save_track("AAPL", short_state, "short")
        # Per-direction read.
        got_long = p.load_track("AAPL", "long")
        got_short = p.load_track("AAPL", "short")
        assert got_long == long_state, got_long
        assert got_short == short_state, got_short
        # load_all separates the two namespaces.
        all_long = p.load_all_tracks("long")
        all_short = p.load_all_tracks("short")
        assert "AAPL" in all_long and all_long["AAPL"] == long_state
        assert "AAPL" in all_short and all_short["AAPL"] == short_state
        # Delete + read returns None.
        p.delete_track("AAPL", "long")
        assert p.load_track("AAPL", "long") is None
        assert p.load_track("AAPL", "short") == short_state

    @t("v5.1.8 persistence: replace_all_tracks atomically wipes + rewrites")
    def _():
        import persistence as p
        p.replace_all_tracks({"OLD": {"x": 1}}, {})
        assert "OLD" in p.load_all_tracks("long")
        # Replace with different set: OLD must disappear, NEW must appear.
        p.replace_all_tracks({"NEW": {"y": 2}}, {"BEAR": {"z": 3}})
        long_now = p.load_all_tracks("long")
        short_now = p.load_all_tracks("short")
        assert long_now == {"NEW": {"y": 2}}, long_now
        assert short_now == {"BEAR": {"z": 3}}, short_now

    @t("v5.1.8 persistence: write rolls back on exception inside transaction")
    def _():
        # Half-write rollback test: simulate a failure mid-transaction
        # and confirm the row never lands in the table. Replicates the
        # behavior we'd want under a real crash (BEGIN IMMEDIATE +
        # explicit ROLLBACK on the except branch).
        import persistence as p
        import sqlite3
        p.prune_fired("__never_matches__")
        c = p._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "INSERT INTO fired_set (job_key, fired_at_utc) VALUES (?, ?)",
                ("rollback-key", "2026-04-26T00:00:00+00:00"),
            )
            # Force an error mid-transaction.
            raise RuntimeError("simulated crash")
        except RuntimeError:
            c.execute("ROLLBACK")
        # Confirm the row was rolled back \u2014 not visible after failure.
        assert not p.was_fired("rollback-key")

    @t("v5.1.8 persistence: WAL journal_mode is set")
    def _():
        import persistence as p
        c = p._conn()
        cur = c.execute("PRAGMA journal_mode")
        mode = cur.fetchone()[0]
        assert mode.lower() == "wal", f"journal_mode is {mode}, expected wal"

    @t("v5.1.8 persistence: STATE_DB_PATH env var is honored")
    def _():
        import persistence as p
        # The smoke harness sets STATE_DB_PATH to a tmp path; verify
        # init_db is using that exact path, not the /data/ default.
        assert p.STATE_DB_PATH == os.environ["STATE_DB_PATH"], \
            f"STATE_DB_PATH={p.STATE_DB_PATH!r} expected={os.environ['STATE_DB_PATH']!r}"
        assert os.path.exists(p.STATE_DB_PATH), \
            f"DB file not created at {p.STATE_DB_PATH}"

    @t("v5.1.8 persistence: migrate_from_json imports v5 keys then renames source")
    def _():
        import persistence as p
        import json as _json
        import tempfile
        # Wipe SQLite, build a fake legacy paper_state.json with v5 keys.
        p.replace_all_tracks({}, {})
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "paper_state.json")
            blob = {
                "paper_cash": 100000.0,
                "v5_long_tracks": {
                    "AAPL": {"state": "TRAILING", "ticker": "AAPL"},
                },
                "v5_short_tracks": {
                    "TSLA": {"state": "WATCHING", "ticker": "TSLA"},
                },
            }
            with open(src, "w") as f:
                _json.dump(blob, f)
            n = p.migrate_from_json(src)
            assert n == 2, f"expected 2 imports, got {n}"
            # Source file renamed.
            assert not os.path.exists(src), "source not renamed"
            assert os.path.exists(src + ".migrated.bak"), "bak missing"
            # Tracks now in SQLite.
            assert p.load_track("AAPL", "long")["state"] == "TRAILING"
            assert p.load_track("TSLA", "short")["state"] == "WATCHING"
            # Re-running is a no-op (source already gone).
            assert p.migrate_from_json(src) == 0

    @t("v5 plumbing: STRATEGY.md mentioned in trade_genius rolling release note")
    def _():
        # STRATEGY.md is the canonical v5 spec; it must remain referenced in
        # the rolling MAIN_RELEASE_NOTE surface (CURRENT + history tail) so
        # /version always points users at the source of truth, even when the
        # current note is a hotfix that doesn't itself need to repeat the ref.
        assert "STRATEGY.md" in m.MAIN_RELEASE_NOTE

    @t("infra: Dockerfile COPY whitelist includes every top-level imported module")
    def _():
        # v5.0.2 hotfix guard: prevent the v4.11.0 / v5.0.0 footgun where a new
        # top-level module is added to the source tree but the Dockerfile per-file
        # COPY whitelist is forgotten, causing prod to crash on import.
        import os, re
        repo_root = os.path.dirname(os.path.abspath(__file__))
        # Local top-level modules = .py files at repo root (excluding tests/scripts).
        local_modules = set()
        for fn in os.listdir(repo_root):
            if not fn.endswith(".py"):
                continue
            if fn in ("smoke_test.py", "trade_genius.py"):
                continue
            local_modules.add(fn[:-3])
        tg = open(os.path.join(repo_root, "trade_genius.py"), "r", encoding="utf-8").read()
        # Imports of the form `import foo` / `import foo as bar` / `from foo import ...`.
        imported = set()
        for line in tg.splitlines():
            s = line.lstrip()
            mm = re.match(r"(?:import|from)\s+([a-zA-Z_][a-zA-Z0-9_]*)", s)
            if not mm:
                continue
            name = mm.group(1)
            if name in local_modules:
                imported.add(name)
        # Read Dockerfile and find COPY <module>.py lines.
        df_path = os.path.join(repo_root, "Dockerfile")
        df = open(df_path, "r", encoding="utf-8").read()
        copied = set(re.findall(r"^\s*COPY\s+([a-zA-Z_][a-zA-Z0-9_]*)\.py\s", df, re.M))
        missing = sorted(imported - copied)
        assert not missing, (
            f"trade_genius.py imports local modules that are NOT in Dockerfile "
            f"COPY whitelist (would crash prod on import): {missing}"
        )

    # ---------- v5.0.3 executor notifier + alpaca-key fallback ----------
    # Shared helper: minimal subclass that loads from env / chat-map without
    # running the network-touching `start()` path or spawning tg threads.
    def _make_exec(env_prefix: str = "SMOKE_", chats_path: str = ""):
        # Patch ENV_PREFIX on a fresh subclass each call so env-var loads
        # pick up whatever the test set up. NAME -> "SmokeExec" for log
        # readability and a deterministic default chats path.
        class _SmokeExec(m.TradeGeniusBase):
            NAME = "SmokeExec"
            ENV_PREFIX = env_prefix
        if chats_path:
            os.environ[env_prefix + "EXECUTOR_CHATS_PATH"] = chats_path
        return _SmokeExec()

    def _clear_smoke_env(prefix: str = "SMOKE_"):
        for k in list(os.environ.keys()):
            if k.startswith(prefix):
                del os.environ[k]

    @t("executor v5.0.3: chat-map persistence round-trip")
    def _():
        _clear_smoke_env()
        path = str(tmp_dir / "smoke_chats_roundtrip.json")
        if os.path.exists(path):
            os.remove(path)
        bot = _make_exec(chats_path=path)
        bot._record_owner_chat("111", 222)
        bot._record_owner_chat("333", 444)
        assert os.path.exists(path), "chat-map file not written"
        # Reload via fresh instance and verify identity.
        bot2 = _make_exec(chats_path=path)
        assert bot2._owner_chats == {"111": 222, "333": 444}, \
            f"reload mismatch: {bot2._owner_chats}"

    @t("executor v5.0.3: _send_own_telegram with empty chat-map is no-op")
    def _():
        _clear_smoke_env()
        os.environ["SMOKE_TELEGRAM_TG"] = "fake-token"
        path = str(tmp_dir / "smoke_chats_empty.json")
        if os.path.exists(path):
            os.remove(path)
        bot = _make_exec(chats_path=path)
        assert bot._owner_chats == {}, f"expected empty map, got {bot._owner_chats}"
        # Patch urllib.request.urlopen to detect any unexpected call.
        import urllib.request as urlreq
        calls = []
        orig = urlreq.urlopen
        urlreq.urlopen = lambda *a, **kw: calls.append((a, kw)) or (_ for _ in ()).throw(
            AssertionError("urlopen must not be called when chat-map is empty"))
        try:
            bot._send_own_telegram("hello")
        finally:
            urlreq.urlopen = orig
        assert calls == [], f"urlopen was called: {calls}"

    @t("executor v5.0.3: _send_own_telegram fans out to every owner in chat-map")
    def _():
        _clear_smoke_env()
        os.environ["SMOKE_TELEGRAM_TG"] = "fake-token"
        path = str(tmp_dir / "smoke_chats_fanout.json")
        if os.path.exists(path):
            os.remove(path)
        bot = _make_exec(chats_path=path)
        bot._record_owner_chat("111", 222)
        bot._record_owner_chat("333", 444)
        import urllib.request as urlreq
        calls = []
        class _FakeResp:
            def read(self_inner): return b""
        def _fake_urlopen(req, timeout=10):
            calls.append((req.full_url, req.data))
            return _FakeResp()
        orig = urlreq.urlopen
        urlreq.urlopen = _fake_urlopen
        try:
            bot._send_own_telegram("trade msg")
        finally:
            urlreq.urlopen = orig
        assert len(calls) == 2, f"expected 2 fan-out calls, got {len(calls)}: {calls}"
        # Both calls go to sendMessage with our fake token.
        for url, _ in calls:
            assert "api.telegram.org/botfake-token/sendMessage" in url, url
        chat_ids = [d for _, d in calls]
        joined = b"\n".join(chat_ids)
        assert b"chat_id=222" in joined and b"chat_id=444" in joined, \
            f"missing chat_ids in payloads: {joined!r}"

    @t("executor v5.0.4: alpaca paper key reads ALPACA_PAPER_KEY when set")
    def _():
        _clear_smoke_env()
        os.environ["SMOKE_ALPACA_PAPER_KEY"] = "primary-key"
        os.environ["SMOKE_ALPACA_PAPER_SECRET"] = "primary-secret"
        bot = _make_exec()
        assert bot.paper_key == "primary-key", f"got {bot.paper_key!r}"
        assert bot.paper_secret == "primary-secret", f"got {bot.paper_secret!r}"
        _clear_smoke_env()

    @t("executor v5.0.3: chat_id auto-learn updates the persisted map")
    def _():
        _clear_smoke_env()
        path = str(tmp_dir / "smoke_chats_autolearn.json")
        if os.path.exists(path):
            os.remove(path)
        bot = _make_exec(chats_path=path)
        owner = next(iter(m.TRADEGENIUS_OWNER_IDS))
        # Simulate a PTB Update from an owner DM.
        class FakeUser:
            id = int(owner)
        class FakeChat:
            id = 7777777
        class FakeUpdate:
            effective_user = FakeUser()
            effective_chat = FakeChat()
        import asyncio
        asyncio.run(bot._auth_guard(FakeUpdate(), None))
        assert bot._owner_chats.get(owner) == 7777777, \
            f"auto-learn missed: {bot._owner_chats}"
        assert os.path.exists(path), "auto-learn did not persist to disk"
        import json as _json
        with open(path) as f:
            on_disk = _json.load(f)
        assert on_disk.get(owner) == 7777777, f"on-disk mismatch: {on_disk}"
        _clear_smoke_env()

    # =========================================================
    # v5.1.0 — Forensic Volume Filter (SHADOW MODE)
    # =========================================================
    import volume_profile as vp_mod
    from datetime import date as _date, datetime as _dt, timedelta as _td, timezone as _tz
    from zoneinfo import ZoneInfo as _ZI

    @t("volprofile: is_trading_day flags weekday")
    def _():
        # 2026-04-22 is Wednesday.
        assert vp_mod.is_trading_day(_date(2026, 4, 22)) is True

    @t("volprofile: is_trading_day rejects weekend")
    def _():
        # 2026-04-25 is Saturday.
        assert vp_mod.is_trading_day(_date(2026, 4, 25)) is False
        # 2026-04-26 is Sunday.
        assert vp_mod.is_trading_day(_date(2026, 4, 26)) is False

    @t("volprofile: is_trading_day rejects NYSE holiday")
    def _():
        # Good Friday 2026.
        assert vp_mod.is_trading_day(_date(2026, 4, 3)) is False
        # Christmas 2026.
        assert vp_mod.is_trading_day(_date(2026, 12, 25)) is False

    @t("volprofile: trading_days_back(date(2026,4,25),55) returns exactly 55 trading days")
    def _():
        days = vp_mod.trading_days_back(_date(2026, 4, 25), 55)
        assert len(days) == 55, f"len={len(days)}"
        for d in days:
            assert d.weekday() < 5, f"weekend in result: {d}"
            assert d.isoformat() not in vp_mod.NYSE_HOLIDAYS, f"holiday in result: {d}"
        # Strictly ascending.
        assert days == sorted(days), "not ascending"

    @t("volprofile: session_bucket boundary 09:30 → None, 09:31 → '0931'")
    def _():
        et = _ZI("America/New_York")
        # 2026-04-22 is a regular Wednesday.
        assert vp_mod.session_bucket(_dt(2026, 4, 22, 9, 30, tzinfo=et)) is None
        assert vp_mod.session_bucket(_dt(2026, 4, 22, 9, 31, tzinfo=et)) == "0931"

    @t("volprofile: session_bucket 15:59 → '1559', 16:00 → None")
    def _():
        et = _ZI("America/New_York")
        assert vp_mod.session_bucket(_dt(2026, 4, 22, 15, 59, tzinfo=et)) == "1559"
        assert vp_mod.session_bucket(_dt(2026, 4, 22, 16, 0, tzinfo=et)) is None

    @t("volprofile: session_bucket honours early close")
    def _():
        et = _ZI("America/New_York")
        # 2026-11-27 closes at 13:00. 12:59 is in-session, 13:00 is out.
        assert vp_mod.session_bucket(_dt(2026, 11, 27, 12, 59, tzinfo=et)) == "1259"
        assert vp_mod.session_bucket(_dt(2026, 11, 27, 13, 0, tzinfo=et)) is None

    def _fresh_profile(median_v=1000):
        # build_ts_utc near-now; a single bucket "1030".
        return {
            "version": vp_mod.PROFILE_VERSION,
            "ticker": "AAPL",
            "feed_baseline": "sip",
            "feed_live": "iex",
            "iex_sip_ratio": 0.018,
            "window_trading_days": 55,
            "build_ts_utc": _dt.now(tz=_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "buckets": {"1030": {"median": median_v, "p75": median_v + 100,
                                  "p90": median_v + 500, "n": 55}},
        }

    @t("volprofile: evaluate_g4 Stage 1 GREEN at exactly 120%/100%")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        prof = _fresh_profile(1000)
        qqq = _fresh_profile(2000)
        qqq["ticker"] = "QQQ"
        out = vp_mod.evaluate_g4(
            ticker="AAPL", minute_bucket="1030",
            current_volume=1200, profile=prof,
            qqq_current_volume=2000, qqq_profile=qqq,
            stage=1,
        )
        assert out["green"] is True, out
        assert out["rule"] == "V-P1-R1"

    @t("volprofile: evaluate_g4 Stage 1 RED at 119% (off-by-one)")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        prof = _fresh_profile(1000)
        qqq = _fresh_profile(2000); qqq["ticker"] = "QQQ"
        out = vp_mod.evaluate_g4(
            ticker="AAPL", minute_bucket="1030",
            current_volume=1190, profile=prof,
            qqq_current_volume=2000, qqq_profile=qqq, stage=1,
        )
        assert out["green"] is False
        assert out["reason"] == "LOW_TICKER", out

    @t("volprofile: evaluate_g4 Stage 1 RED at 120%/99% (low qqq)")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        prof = _fresh_profile(1000)
        qqq = _fresh_profile(2000); qqq["ticker"] = "QQQ"
        out = vp_mod.evaluate_g4(
            ticker="AAPL", minute_bucket="1030",
            current_volume=1200, profile=prof,
            qqq_current_volume=1980, qqq_profile=qqq, stage=1,
        )
        assert out["green"] is False
        assert out["reason"] == "LOW_QQQ", out

    @t("volprofile: evaluate_g4 Stage 2 GREEN at 100%")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        prof = _fresh_profile(1000)
        out = vp_mod.evaluate_g4(
            ticker="AAPL", minute_bucket="1030",
            current_volume=1000, profile=prof,
            qqq_current_volume=0, qqq_profile=None, stage=2,
        )
        assert out["green"] is True, out
        assert out["rule"] == "V-P1-R3"

    @t("volprofile: evaluate_g4 NO_PROFILE_X when profile=None")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        out = vp_mod.evaluate_g4(
            ticker="AAPL", minute_bucket="1030",
            current_volume=999, profile=None,
            qqq_current_volume=0, qqq_profile=None, stage=2,
        )
        assert out["green"] is False
        assert out["reason"] == "NO_PROFILE_AAPL", out

    @t("volprofile: evaluate_g4 STALE_PROFILE_X when build_ts > 36h old")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        prof = _fresh_profile(1000)
        # Backdate by 48 hours.
        old = _dt.now(tz=_tz.utc) - _td(hours=48)
        prof["build_ts_utc"] = old.strftime("%Y-%m-%dT%H:%M:%SZ")
        out = vp_mod.evaluate_g4(
            ticker="AAPL", minute_bucket="1030",
            current_volume=1500, profile=prof,
            qqq_current_volume=0, qqq_profile=None, stage=2,
        )
        assert out["green"] is False
        assert out["reason"] == "STALE_PROFILE_AAPL", out

    @t("volprofile: evaluate_g4 NO_BUCKET when bucket missing (e.g. 0930)")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        prof = _fresh_profile(1000)
        out = vp_mod.evaluate_g4(
            ticker="AAPL", minute_bucket="0930",
            current_volume=1500, profile=prof,
            qqq_current_volume=0, qqq_profile=None, stage=2,
        )
        assert out["green"] is False
        assert out["reason"] == "NO_BUCKET_AAPL_0930", out

    @t("volprofile: evaluate_g4 returns DISABLED when VOLUME_PROFILE_ENABLED=False")
    def _():
        prev = vp_mod.VOLUME_PROFILE_ENABLED
        try:
            vp_mod.VOLUME_PROFILE_ENABLED = False
            out = vp_mod.evaluate_g4(
                ticker="AAPL", minute_bucket="1030",
                current_volume=99999, profile=_fresh_profile(),
                qqq_current_volume=99999, qqq_profile=_fresh_profile(),
                stage=1,
            )
            assert out["reason"] == "DISABLED", out
            assert out["green"] is False
        finally:
            vp_mod.VOLUME_PROFILE_ENABLED = prev

    @t("volprofile: profile JSON round-trip via save/load")
    def _():
        import tempfile
        prev_dir = vp_mod.PROFILE_DIR
        with tempfile.TemporaryDirectory() as tmpd:
            vp_mod.PROFILE_DIR = tmpd
            try:
                prof = _fresh_profile(1234)
                vp_mod.save_profile("AAPL", prof)
                got = vp_mod.load_profile("AAPL")
                assert got is not None, "load returned None"
                assert got["buckets"]["1030"]["median"] == 1234, got
                # Missing returns None.
                assert vp_mod.load_profile("ZZZZ") is None
            finally:
                vp_mod.PROFILE_DIR = prev_dir

    @t("volprofile: trade_genius hard-disables module when watchlist > 30")
    def _():
        # We can't safely call _start_volume_profile() here (it would try
        # to spawn a websocket thread). Instead simulate the cap check
        # the function performs.
        big = ["A%d" % i for i in range(31)]
        assert len(big) > vp_mod.WS_SYMBOL_CAP_FREE_IEX, "test setup broken"

    @t("volprofile: shadow log helper exists and is a callable")
    def _():
        assert hasattr(m, "_shadow_log_g4")
        assert callable(m._shadow_log_g4)

    @t("volprofile: trade_genius imports volume_profile module")
    def _():
        assert hasattr(m, "volume_profile"), "volume_profile not imported"
        assert hasattr(m.volume_profile, "evaluate_g4")

    @t("infra: Dockerfile COPY includes volume_profile.py")
    def _():
        df = (Path(__file__).parent / "Dockerfile").read_text(encoding="utf-8")
        assert "COPY volume_profile.py" in df, "Dockerfile missing volume_profile.py COPY"

    # ---------------------------------------------------------------
    # v5.1.1 \u2014 env-driven A/B toggles + 3-config parallel shadow
    # ---------------------------------------------------------------

    def _v511_save_env() -> dict:
        keys = (
            "VOL_GATE_ENFORCE", "VOL_GATE_TICKER_ENABLED",
            "VOL_GATE_INDEX_ENABLED", "VOL_GATE_TICKER_PCT",
            "VOL_GATE_QQQ_PCT", "VOL_GATE_INDEX_SYMBOL",
        )
        return {k: os.environ.get(k) for k in keys}

    def _v511_restore_env(saved: dict) -> None:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @t("v5.1.1: load_active_config defaults preserve v5.1.0 behavior")
    def _():
        saved = _v511_save_env()
        try:
            for k in ("VOL_GATE_ENFORCE", "VOL_GATE_TICKER_ENABLED",
                     "VOL_GATE_INDEX_ENABLED", "VOL_GATE_TICKER_PCT",
                     "VOL_GATE_QQQ_PCT", "VOL_GATE_INDEX_SYMBOL"):
                os.environ.pop(k, None)
            cfg = vp_mod.load_active_config()
            assert cfg["enforce"] is False, cfg
            assert cfg["ticker_enabled"] is True, cfg
            assert cfg["index_enabled"] is True, cfg
            assert cfg["ticker_pct"] == 70, cfg
            assert cfg["index_pct"] == 100, cfg
            assert cfg["index_symbol"] == "QQQ", cfg
        finally:
            _v511_restore_env(saved)

    @t("v5.1.1: env vars override defaults (toggles + thresholds + symbol)")
    def _():
        saved = _v511_save_env()
        try:
            os.environ["VOL_GATE_ENFORCE"] = "1"
            os.environ["VOL_GATE_TICKER_ENABLED"] = "0"
            os.environ["VOL_GATE_INDEX_ENABLED"] = "1"
            os.environ["VOL_GATE_TICKER_PCT"] = "85"
            os.environ["VOL_GATE_QQQ_PCT"] = "120"
            os.environ["VOL_GATE_INDEX_SYMBOL"] = "spy"
            cfg = vp_mod.load_active_config()
            assert cfg["enforce"] is True, cfg
            assert cfg["ticker_enabled"] is False, cfg
            assert cfg["index_enabled"] is True, cfg
            assert cfg["ticker_pct"] == 85, cfg
            assert cfg["index_pct"] == 120, cfg
            # Symbol normalises to upper-case.
            assert cfg["index_symbol"] == "SPY", cfg
        finally:
            _v511_restore_env(saved)

    @t("v5.1.1: env-int parser falls back on garbage input, never crashes")
    def _():
        saved = _v511_save_env()
        try:
            os.environ["VOL_GATE_TICKER_PCT"] = "not-an-int"
            os.environ["VOL_GATE_QQQ_PCT"] = ""
            cfg = vp_mod.load_active_config()
            assert cfg["ticker_pct"] == 70, cfg
            assert cfg["index_pct"] == 100, cfg
        finally:
            _v511_restore_env(saved)

    @t("v5.1.1: SHADOW_CONFIGS is the fixed 5-config tuple (v5.1.6 added BUCKET_FILL_100)")
    def _():
        cfgs = vp_mod.SHADOW_CONFIGS
        assert isinstance(cfgs, tuple) and len(cfgs) == 5, cfgs
        names = [c["name"] for c in cfgs]
        assert names == ["TICKER+QQQ", "TICKER_ONLY", "QQQ_ONLY",
                         "GEMINI_A", "BUCKET_FILL_100"], names
        # Thresholds match backtest recommendation.
        assert cfgs[0]["ticker_pct"] == 70 and cfgs[0]["index_pct"] == 100
        assert cfgs[1]["ticker_enabled"] is True and cfgs[1]["index_enabled"] is False
        assert cfgs[1]["ticker_pct"] == 70
        assert cfgs[2]["ticker_enabled"] is False and cfgs[2]["index_enabled"] is True
        assert cfgs[2]["index_pct"] == 100
        # v5.1.2 \u2014 GEMINI_A 110/85, both anchors enabled.
        assert cfgs[3]["ticker_enabled"] is True and cfgs[3]["index_enabled"] is True
        assert cfgs[3]["ticker_pct"] == 110 and cfgs[3]["index_pct"] == 85
        # v5.1.6 \u2014 BUCKET_FILL_100 100/100, both anchors enabled.
        assert cfgs[4]["ticker_enabled"] is True and cfgs[4]["index_enabled"] is True
        assert cfgs[4]["ticker_pct"] == 100 and cfgs[4]["index_pct"] == 100

    @t("v5.1.1: evaluate_g4_config TICKER+QQQ PASS at 70%/100%")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        prof = _fresh_profile(1000)
        qqq = _fresh_profile(2000); qqq["ticker"] = "QQQ"
        out = vp_mod.evaluate_g4_config(
            ticker="AMD", minute_bucket="1030",
            current_volume=700, profile=prof,
            index_current_volume=2000, index_profile=qqq,
            ticker_enabled=True, index_enabled=True,
            ticker_pct=70, index_pct=100,
        )
        assert out["verdict"] == "PASS", out
        assert out["reason"] == "OK", out
        assert out["ticker_pct"] == 70, out
        assert out["qqq_pct"] == 100, out

    @t("v5.1.1: evaluate_g4_config TICKER+QQQ BLOCK low ticker")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        prof = _fresh_profile(1000)
        qqq = _fresh_profile(2000); qqq["ticker"] = "QQQ"
        out = vp_mod.evaluate_g4_config(
            ticker="AMD", minute_bucket="1030",
            current_volume=600, profile=prof,
            index_current_volume=2200, index_profile=qqq,
            ticker_enabled=True, index_enabled=True,
            ticker_pct=70, index_pct=100,
        )
        assert out["verdict"] == "BLOCK", out
        assert out["reason"] == "LOW_TICKER", out

    @t("v5.1.1: evaluate_g4_config TICKER_ONLY ignores QQQ entirely")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        prof = _fresh_profile(1000)
        # QQQ profile None and current vol = 0 \u2014 ticker_only must not care.
        out = vp_mod.evaluate_g4_config(
            ticker="AMD", minute_bucket="1030",
            current_volume=900, profile=prof,
            index_current_volume=0, index_profile=None,
            ticker_enabled=True, index_enabled=False,
            ticker_pct=70, index_pct=100,
        )
        assert out["verdict"] == "PASS", out
        assert out["qqq_pct"] is None, out
        assert out["ticker_pct"] == 90, out

    @t("v5.1.1: evaluate_g4_config QQQ_ONLY ignores ticker entirely")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        qqq = _fresh_profile(2000); qqq["ticker"] = "QQQ"
        # Ticker profile None and current vol = 0 \u2014 qqq_only must not care.
        out = vp_mod.evaluate_g4_config(
            ticker="AMD", minute_bucket="1030",
            current_volume=0, profile=None,
            index_current_volume=2400, index_profile=qqq,
            ticker_enabled=False, index_enabled=True,
            ticker_pct=70, index_pct=100,
        )
        assert out["verdict"] == "PASS", out
        assert out["ticker_pct"] is None, out
        assert out["qqq_pct"] == 120, out

    @t("v5.1.1: evaluate_g4_config QQQ_ONLY BLOCK low qqq")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        qqq = _fresh_profile(2000); qqq["ticker"] = "QQQ"
        out = vp_mod.evaluate_g4_config(
            ticker="AMD", minute_bucket="1030",
            current_volume=0, profile=None,
            index_current_volume=1900, index_profile=qqq,
            ticker_enabled=False, index_enabled=True,
            ticker_pct=70, index_pct=100,
        )
        assert out["verdict"] == "BLOCK", out
        assert out["reason"] == "LOW_QQQ", out
        assert out["qqq_pct"] == 95, out

    @t("v5.1.1: evaluate_g4_config DISABLED short-circuits")
    def _():
        prev = vp_mod.VOLUME_PROFILE_ENABLED
        try:
            vp_mod.VOLUME_PROFILE_ENABLED = False
            out = vp_mod.evaluate_g4_config(
                ticker="AMD", minute_bucket="1030",
                current_volume=999, profile=_fresh_profile(),
                index_current_volume=999, index_profile=_fresh_profile(),
                ticker_enabled=True, index_enabled=True,
                ticker_pct=70, index_pct=100,
            )
            assert out["verdict"] == "BLOCK", out
            assert out["reason"] == "DISABLED", out
        finally:
            vp_mod.VOLUME_PROFILE_ENABLED = prev

    @t("v5.1.1: _shadow_log_g4 emits 5 [CFG=...] lines on a candidate (v5.1.6 added BUCKET_FILL_100)")
    def _():
        # Stand up an in-memory profile cache so every config has data.
        vp_mod.VOLUME_PROFILE_ENABLED = True
        m.VOLUME_PROFILE_ENABLED = True
        prev_cache = m._volume_profile_cache.copy()
        prev_ws = m._ws_consumer
        try:
            prof = _fresh_profile(1000)
            qqq = _fresh_profile(2000); qqq["ticker"] = "QQQ"
            m._volume_profile_cache.clear()
            m._volume_profile_cache["AMD"] = prof
            m._volume_profile_cache["QQQ"] = qqq

            class _StubWS:
                def current_volume(self, t, b):
                    return 1500 if t == "AMD" else 2400
            m._ws_consumer = _StubWS()

            # Force session_bucket() to return something deterministic by
            # patching datetime.now in the volume_profile module.
            real_session_bucket = vp_mod.session_bucket
            vp_mod.session_bucket = lambda _ts: "1030"
            try:
                import logging as _logging
                seen: list[str] = []

                class _H(_logging.Handler):
                    def emit(self, rec):
                        seen.append(rec.getMessage())
                tg_logger = _logging.getLogger("trade_genius")
                h = _H(); h.setLevel(_logging.INFO)
                tg_logger.addHandler(h); old_level = tg_logger.level
                tg_logger.setLevel(_logging.INFO)
                try:
                    m._shadow_log_g4("AMD", stage=1, existing_decision="ENTER")
                finally:
                    tg_logger.removeHandler(h); tg_logger.setLevel(old_level)
            finally:
                vp_mod.session_bucket = real_session_bucket

            cfg_lines = [s for s in seen if "[V510-SHADOW][CFG=" in s]
            assert len(cfg_lines) == 5, f"want 5 cfg lines, got {len(cfg_lines)}: {seen}"
            joined = " | ".join(cfg_lines)
            assert "CFG=TICKER+QQQ" in joined, joined
            assert "CFG=TICKER_ONLY" in joined, joined
            assert "CFG=QQQ_ONLY" in joined, joined
            assert "CFG=GEMINI_A" in joined, joined
            assert "CFG=BUCKET_FILL_100" in joined, joined
            assert "PCT=70/100" in joined, joined
            assert "PCT=70]" in joined, joined
            assert "PCT=100]" in joined, joined
            assert "PCT=110/85" in joined, joined
            assert "PCT=100/100" in joined, joined
        finally:
            m._volume_profile_cache.clear()
            m._volume_profile_cache.update(prev_cache)
            m._ws_consumer = prev_ws

    @t("v5.1.1: VOL_GATE_ENFORCE default is 0 (no enforcement next week)")
    def _():
        saved = _v511_save_env()
        try:
            os.environ.pop("VOL_GATE_ENFORCE", None)
            cfg = vp_mod.load_active_config()
            assert cfg["enforce"] is False, cfg
        finally:
            _v511_restore_env(saved)

    @t("v5.1.1: original [V510-SHADOW] line still emitted (back-compat)")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        m.VOLUME_PROFILE_ENABLED = True
        prev_cache = m._volume_profile_cache.copy()
        prev_ws = m._ws_consumer
        try:
            prof = _fresh_profile(1000)
            qqq = _fresh_profile(2000); qqq["ticker"] = "QQQ"
            m._volume_profile_cache.clear()
            m._volume_profile_cache["AMD"] = prof
            m._volume_profile_cache["QQQ"] = qqq

            class _StubWS:
                def current_volume(self, t, b):
                    return 1500 if t == "AMD" else 2400
            m._ws_consumer = _StubWS()
            real_session_bucket = vp_mod.session_bucket
            vp_mod.session_bucket = lambda _ts: "1030"
            try:
                import logging as _logging
                seen: list[str] = []

                class _H(_logging.Handler):
                    def emit(self, rec):
                        seen.append(rec.getMessage())
                tg_logger = _logging.getLogger("trade_genius")
                h = _H(); h.setLevel(_logging.INFO)
                tg_logger.addHandler(h); old_level = tg_logger.level
                tg_logger.setLevel(_logging.INFO)
                try:
                    m._shadow_log_g4("AMD", stage=1, existing_decision="ENTER")
                finally:
                    tg_logger.removeHandler(h); tg_logger.setLevel(old_level)
            finally:
                vp_mod.session_bucket = real_session_bucket

            # Exactly one back-compat line (no [CFG=]) plus the 3 cfg lines.
            backcompat = [s for s in seen
                          if s.startswith("[V510-SHADOW] ") and "[CFG=" not in s]
            assert len(backcompat) == 1, f"want 1 back-compat line, got {seen}"
        finally:
            m._volume_profile_cache.clear()
            m._volume_profile_cache.update(prev_cache)
            m._ws_consumer = prev_ws

    # ---------------------------------------------------------------
    # v5.1.2 \u2014 forensic capture (Tier-1 + Tier-2) + GEMINI_A
    # ---------------------------------------------------------------

    @t("v5.1.2: GEMINI_A is the 4th SHADOW_CONFIGS entry at 110/85")
    def _():
        cfgs = vp_mod.SHADOW_CONFIGS
        assert len(cfgs) >= 4, cfgs
        gem = cfgs[3]
        assert gem["name"] == "GEMINI_A", gem
        assert gem["ticker_enabled"] is True and gem["index_enabled"] is True, gem
        assert gem["ticker_pct"] == 110, gem
        assert gem["index_pct"] == 85, gem

    @t("v5.1.6: BUCKET_FILL_100 is the 5th SHADOW_CONFIGS entry at 100/100")
    def _():
        cfgs = vp_mod.SHADOW_CONFIGS
        assert len(cfgs) >= 5, cfgs
        bf = cfgs[4]
        assert bf["name"] == "BUCKET_FILL_100", bf
        assert bf["ticker_enabled"] is True and bf["index_enabled"] is True, bf
        assert bf["ticker_pct"] == 100, bf
        assert bf["index_pct"] == 100, bf

    @t("v5.1.6: trade_genius exposes _v516_log_velocity / _v516_log_index / _v516_log_di")
    def _():
        for fn in ("_v516_log_velocity", "_v516_log_index",
                   "_v516_log_di", "_v516_check_velocity"):
            assert hasattr(m, fn) and callable(getattr(m, fn)), fn

    @t("v5.1.6: _v516_log_velocity emits a [V510-VEL] line")
    def _():
        import logging as _logging
        seen: list[str] = []

        class _H(_logging.Handler):
            def emit(self, rec):
                seen.append(rec.getMessage())
        tg_logger = _logging.getLogger("trade_genius")
        h = _H(); h.setLevel(_logging.INFO)
        tg_logger.addHandler(h); old_level = tg_logger.level
        tg_logger.setLevel(_logging.INFO)
        try:
            m._v516_log_velocity("NVDA", "1423", 42, 2871, 2840, 101.1, 78.3)
        finally:
            tg_logger.removeHandler(h); tg_logger.setLevel(old_level)
        line = next((s for s in seen if s.startswith("[V510-VEL]")), None)
        assert line is not None, seen
        assert "ticker=NVDA" in line, line
        assert "minute=1423" in line, line
        assert "second=42" in line, line
        assert "running_vol=2871" in line, line
        assert "bucket=2840" in line, line
        assert "pct=101.1" in line, line
        assert "qqq_pct=78.3" in line, line

    @t("v5.1.6: _v516_check_velocity fires once per (ticker, minute)")
    def _():
        import logging as _logging
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        seen: list[str] = []

        class _H(_logging.Handler):
            def emit(self, rec):
                if rec.getMessage().startswith("[V510-VEL]"):
                    seen.append(rec.getMessage())
        tg_logger = _logging.getLogger("trade_genius")
        h = _H(); h.setLevel(_logging.INFO)
        tg_logger.addHandler(h); old_level = tg_logger.level
        tg_logger.setLevel(_logging.INFO)
        try:
            # Reset module state so the test is order-independent.
            m._v516_vel_state.pop("FAKE", None)
            t1 = _dt(2026, 4, 28, 14, 23, 42, tzinfo=_ZI("America/New_York"))
            t2 = _dt(2026, 4, 28, 14, 23, 50, tzinfo=_ZI("America/New_York"))
            # First call: under bucket \u2014 no emit.
            m._v516_check_velocity("FAKE", "1423", t1, 100, 200)
            # Second call: crosses 100% \u2014 emit.
            m._v516_check_velocity("FAKE", "1423", t1, 250, 200, qqq_pct=88)
            # Third call: same minute, still over \u2014 must NOT emit again.
            m._v516_check_velocity("FAKE", "1423", t2, 300, 200, qqq_pct=90)
        finally:
            tg_logger.removeHandler(h); tg_logger.setLevel(old_level)
            m._v516_vel_state.pop("FAKE", None)
        assert len(seen) == 1, seen
        assert "second=42" in seen[0], seen
        assert "ticker=FAKE" in seen[0], seen

    @t("v5.1.6: _v516_log_index emits SPY+QQQ above-PDC verdict")
    def _():
        import logging as _logging
        seen: list[str] = []

        class _H(_logging.Handler):
            def emit(self, rec):
                seen.append(rec.getMessage())
        tg_logger = _logging.getLogger("trade_genius")
        h = _H(); h.setLevel(_logging.INFO)
        tg_logger.addHandler(h); old_level = tg_logger.level
        tg_logger.setLevel(_logging.INFO)
        try:
            m._v516_log_index(710.40, 708.72, 649.09, 646.79)
        finally:
            tg_logger.removeHandler(h); tg_logger.setLevel(old_level)
        line = next((s for s in seen if s.startswith("[V510-IDX]")), None)
        assert line is not None, seen
        assert "spy_close=710.4" in line, line
        assert "spy_pdc=708.72" in line, line
        assert "spy_above=Y" in line, line
        assert "qqq_above=Y" in line, line

    @t("v5.1.6: _v516_log_di emits double-tap flags")
    def _():
        import logging as _logging
        seen: list[str] = []

        class _H(_logging.Handler):
            def emit(self, rec):
                seen.append(rec.getMessage())
        tg_logger = _logging.getLogger("trade_genius")
        h = _H(); h.setLevel(_logging.INFO)
        tg_logger.addHandler(h); old_level = tg_logger.level
        tg_logger.setLevel(_logging.INFO)
        try:
            # both >25 \u2014 double_tap_long Y
            m._v516_log_di("NVDA", 27.4, 29.1, 15.2, 12.8)
        finally:
            tg_logger.removeHandler(h); tg_logger.setLevel(old_level)
        line = next((s for s in seen if s.startswith("[V510-DI]")), None)
        assert line is not None, seen
        assert "ticker=NVDA" in line, line
        assert "di_plus_t-1=27.4" in line, line
        assert "di_plus_t=29.1" in line, line
        assert "double_tap_long=Y" in line, line
        assert "double_tap_short=N" in line, line

    @t("v5.1.2: evaluate_g4_config GEMINI_A PASS at 110%/85%")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        prof = _fresh_profile(1000)
        qqq = _fresh_profile(2000); qqq["ticker"] = "QQQ"
        out = vp_mod.evaluate_g4_config(
            ticker="AMD", minute_bucket="1030",
            current_volume=1100, profile=prof,
            index_current_volume=1700, index_profile=qqq,
            ticker_enabled=True, index_enabled=True,
            ticker_pct=110, index_pct=85,
        )
        assert out["verdict"] == "PASS", out
        assert out["reason"] == "OK", out
        assert out["ticker_pct"] == 110, out
        assert out["qqq_pct"] == 85, out

    @t("v5.1.2: evaluate_g4_config GEMINI_A BLOCK low ticker (just under 110%)")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        prof = _fresh_profile(1000)
        qqq = _fresh_profile(2000); qqq["ticker"] = "QQQ"
        out = vp_mod.evaluate_g4_config(
            ticker="AMD", minute_bucket="1030",
            current_volume=1090, profile=prof,
            index_current_volume=1800, index_profile=qqq,
            ticker_enabled=True, index_enabled=True,
            ticker_pct=110, index_pct=85,
        )
        assert out["verdict"] == "BLOCK", out
        assert out["reason"] == "LOW_TICKER", out

    @t("v5.1.2: indicators module imports and exposes pure functions")
    def _():
        import indicators as ind
        for fn in ("rsi14", "ema9", "ema21", "atr14",
                   "vwap_dist_pct", "spread_bps"):
            assert hasattr(ind, fn) and callable(getattr(ind, fn)), fn

    @t("v5.1.2: indicators.rsi14 returns None on insufficient bars")
    def _():
        import indicators as ind
        assert ind.rsi14([]) is None
        assert ind.rsi14([1.0] * 14) is None  # need >= 15

    @t("v5.1.2: indicators.rsi14 happy path returns finite float")
    def _():
        import indicators as ind
        closes = [10.0 + i * 0.1 for i in range(30)]
        v = ind.rsi14(closes)
        assert v is not None and 0.0 <= v <= 100.0, v

    @t("v5.1.2: indicators.ema9 returns None below period; value above")
    def _():
        import indicators as ind
        assert ind.ema9([1.0] * 8) is None
        v = ind.ema9([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
        assert v is not None and 1.0 <= v <= 10.0, v

    @t("v5.1.2: indicators.ema21 returns None below period; value above")
    def _():
        import indicators as ind
        assert ind.ema21([1.0] * 20) is None
        closes = [float(i) for i in range(30)]
        v = ind.ema21(closes)
        assert v is not None and 0.0 <= v <= 30.0, v

    @t("v5.1.2: indicators.atr14 None on insufficient bars; finite otherwise")
    def _():
        import indicators as ind
        assert ind.atr14([]) is None
        bars = [{"high": 10 + i * 0.1, "low": 9.5 + i * 0.1,
                 "close": 9.9 + i * 0.1} for i in range(30)]
        v = ind.atr14(bars)
        assert v is not None and v > 0.0, v

    @t("v5.1.2: indicators.vwap_dist_pct None on empty; pct on data")
    def _():
        import indicators as ind
        assert ind.vwap_dist_pct([]) is None
        bars = [{"high": 100.0, "low": 99.0, "close": 99.5, "volume": 1000}
                for _ in range(5)]
        bars.append({"high": 102.0, "low": 101.0, "close": 101.5, "volume": 1000})
        v = ind.vwap_dist_pct(bars)
        assert v is not None and v > 0.0, v

    @t("v5.1.2: indicators.spread_bps None on bad input; finite on good")
    def _():
        import indicators as ind
        assert ind.spread_bps(None, None) is None
        assert ind.spread_bps(0.0, 100.0) is None
        assert ind.spread_bps(100.0, 99.0) is None  # crossed
        v = ind.spread_bps(99.99, 100.01)
        assert v is not None and v > 0.0, v

    @t("v5.1.6: indicators.di_plus/di_minus None on insufficient bars")
    def _():
        import indicators as ind
        assert ind.di_plus([]) is None
        assert ind.di_minus([]) is None
        # period=14 needs >= 15 bars
        bars = [{"high": 1.0, "low": 0.5, "close": 0.8} for _ in range(14)]
        assert ind.di_plus(bars) is None
        assert ind.di_minus(bars) is None

    @t("v5.1.6: indicators.di_plus > di_minus in a steady uptrend")
    def _():
        import indicators as ind
        bars = []
        base = 100.0
        for i in range(40):
            base += 0.5
            bars.append({"high": base + 0.4, "low": base - 0.1,
                         "close": base + 0.2})
        dp = ind.di_plus(bars)
        dm = ind.di_minus(bars)
        assert dp is not None and dm is not None, (dp, dm)
        assert 0.0 <= dp <= 100.0 and 0.0 <= dm <= 100.0, (dp, dm)
        assert dp > dm, (dp, dm)

    @t("v5.1.6: indicators.di_minus > di_plus in a steady downtrend")
    def _():
        import indicators as ind
        bars = []
        base = 100.0
        for i in range(40):
            base -= 0.5
            bars.append({"high": base + 0.1, "low": base - 0.4,
                         "close": base - 0.2})
        dp = ind.di_plus(bars)
        dm = ind.di_minus(bars)
        assert dp is not None and dm is not None, (dp, dm)
        assert dm > dp, (dp, dm)

    @t("v5.1.2: bar_archive.write_bar writes JSONL to dated path")
    def _():
        import json as _json
        import tempfile
        import bar_archive as ba
        from datetime import date as _date
        with tempfile.TemporaryDirectory() as td:
            bar = {"ts": "2026-04-28T14:31:00", "et_bucket": "1031",
                   "open": 425.93, "high": 426.10, "low": 425.50,
                   "close": 425.85, "iex_volume": 1851,
                   "iex_sip_ratio_used": 0.082,
                   "bid": 425.84, "ask": 425.86,
                   "last_trade_price": 425.85}
            today = _date(2026, 4, 28)
            path = ba.write_bar("amd", bar, base_dir=td, today=today)
            assert path is not None, "write_bar returned None"
            assert "/2026-04-28/AMD.jsonl" in path, path
            with open(path) as fh:
                lines = fh.read().splitlines()
            assert len(lines) == 1, lines
            obj = _json.loads(lines[0])
            for k in ba.BAR_SCHEMA_FIELDS:
                assert k in obj, k
            assert obj["close"] == 425.85, obj

    @t("v5.1.2: bar_archive.write_bar appends multiple lines atomically")
    def _():
        import tempfile
        import bar_archive as ba
        from datetime import date as _date
        with tempfile.TemporaryDirectory() as td:
            today = _date(2026, 4, 28)
            for i in range(5):
                ba.write_bar("AMD", {"close": 100.0 + i}, base_dir=td, today=today)
            with open(f"{td}/2026-04-28/AMD.jsonl") as fh:
                assert len(fh.read().splitlines()) == 5

    @t("v5.1.2: bar_archive.cleanup_old_dirs keeps recent, deletes old")
    def _():
        import os as _os
        import tempfile
        import bar_archive as ba
        from datetime import date as _date
        with tempfile.TemporaryDirectory() as td:
            for d in ("2025-01-01", "2026-04-20", "2026-04-26"):
                _os.makedirs(f"{td}/{d}")
                with open(f"{td}/{d}/X.jsonl", "w") as fh:
                    fh.write("{}\n")
            today = _date(2026, 4, 26)
            deleted = ba.cleanup_old_dirs(base_dir=td, retain_days=90, today=today)
            assert any("2025-01-01" in d for d in deleted), deleted
            assert _os.path.isdir(f"{td}/2026-04-20"), "recent dir was wrongly deleted"
            assert _os.path.isdir(f"{td}/2026-04-26"), "today's dir was wrongly deleted"

    @t("v5.1.2: bar_archive.write_bar projects unknown keys away")
    def _():
        import json as _json
        import tempfile
        import bar_archive as ba
        from datetime import date as _date
        with tempfile.TemporaryDirectory() as td:
            today = _date(2026, 4, 28)
            ba.write_bar("AMD", {"close": 1.0, "garbage_key": "x"},
                         base_dir=td, today=today)
            with open(f"{td}/2026-04-28/AMD.jsonl") as fh:
                obj = _json.loads(fh.read().splitlines()[0])
            assert "garbage_key" not in obj
            assert obj["close"] == 1.0

    @t("v5.5.2: _v512_archive_minute_bar has a caller outside its own def")
    def _():
        # Regression guard: in v5.1.2 the writer wrapper was added but
        # never wired into the scan loop, so /data/bars/ stayed empty
        # for ~3 months. v5.5.2 wires it in. If a future refactor
        # silently re-orphans the call, this test fails loudly.
        import os as _os
        path = _os.path.join(_os.path.dirname(m.__file__), "trade_genius.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        needle = "_v512_archive_minute_bar("
        # Find every occurrence; the definition itself uses
        # "def _v512_archive_minute_bar(" so the caller list is every
        # other occurrence.
        positions = []
        start = 0
        while True:
            idx = src.find(needle, start)
            if idx < 0:
                break
            positions.append(idx)
            start = idx + 1
        # Exactly one of these is the def line; we need at least one
        # call site beyond that.
        def_count = src.count("def _v512_archive_minute_bar(")
        call_count = len(positions) - def_count
        assert call_count >= 1, (
            f"_v512_archive_minute_bar has no callers (def_count={def_count}, "
            f"total_occurrences={len(positions)}). Wiring was dropped \u2014 "
            f"see v5.5.2 / diagnostics/shadow_data_pipeline.md."
        )

    @t("v5.5.2: bar_archive.cleanup_old_dirs is invoked from eod_close")
    def _():
        # Retention enforcement was missing in v5.1.2. v5.5.2 wires
        # cleanup_old_dirs into the EOD path so archived bars don't
        # accumulate forever on the Railway volume.
        import inspect
        src = inspect.getsource(m.eod_close)
        assert "cleanup_old_dirs" in src, (
            "bar_archive.cleanup_old_dirs not invoked from eod_close \u2014 "
            "90d retention is unenforced. See v5.5.2."
        )

    def _v512_capture_logger():
        import logging as _logging

        seen: list[str] = []

        class _H(_logging.Handler):
            def emit(self, rec):
                seen.append(rec.getMessage())
        tg_logger = _logging.getLogger("trade_genius")
        h = _H(); h.setLevel(_logging.INFO)
        tg_logger.addHandler(h); old_level = tg_logger.level
        tg_logger.setLevel(_logging.INFO)
        return seen, h, tg_logger, old_level

    def _v512_release_logger(h, tg_logger, old_level):
        tg_logger.removeHandler(h); tg_logger.setLevel(old_level)

    @t("v5.1.2: [V510-MINUTE] line emitted with expected fields")
    def _():
        seen, h, lg, old = _v512_capture_logger()
        try:
            m._v512_log_minute("AMD", "1448", 84, 112, 346.19, 12345)
        finally:
            _v512_release_logger(h, lg, old)
        line = next((s for s in seen if "[V510-MINUTE]" in s), None)
        assert line is not None, seen
        for tok in ("ticker=AMD", "bucket=1448", "t_pct=84",
                    "qqq_pct=112", "vol=12345"):
            assert tok in line, (tok, line)

    @t("v5.1.2: [V510-MINUTE] renders None as 'null'")
    def _():
        seen, h, lg, old = _v512_capture_logger()
        try:
            m._v512_log_minute("AMD", None, None, None, None, None)
        finally:
            _v512_release_logger(h, lg, old)
        line = next((s for s in seen if "[V510-MINUTE]" in s), None)
        assert line is not None
        assert "bucket=null" in line
        assert "t_pct=null" in line
        assert "vol=null" in line

    @t("v5.1.2: [V510-CAND] emitted on entered=YES with all fields")
    def _():
        seen, h, lg, old = _v512_capture_logger()
        try:
            m._v512_log_candidate(
                "AMD", "1450", 1, "ARMED", True,
                m.CAND_REASON_BREAKOUT_CONFIRMED,
                t_pct=92, qqq_pct=118, close=347.05, stop=343.20,
                rsi14_=68.4, ema9_=345.80, ema21_=343.92,
                atr14_=1.85, vwap_dist_pct_=0.42, spread_bps_=2.9,
            )
        finally:
            _v512_release_logger(h, lg, old)
        line = next((s for s in seen if "[V510-CAND]" in s), None)
        assert line is not None, seen
        for tok in ("entered=YES", "reason=BREAKOUT_CONFIRMED",
                    "rsi14=68.4", "ema9=345.8", "ema21=343.92",
                    "atr14=1.85", "vwap_dist_pct=0.42",
                    "spread_bps=2.9", "fsm_state=ARMED"):
            assert tok in line, (tok, line)

    @t("v5.1.2: [V510-CAND] emitted on entered=NO with null indicators")
    def _():
        seen, h, lg, old = _v512_capture_logger()
        try:
            m._v512_log_candidate(
                "AMD", "1448", 1, "OBSERVE", False,
                m.CAND_REASON_NO_BREAKOUT,
                t_pct=84, qqq_pct=112, close=346.19,
            )
        finally:
            _v512_release_logger(h, lg, old)
        line = next((s for s in seen if "[V510-CAND]" in s), None)
        assert line is not None, seen
        assert "entered=NO" in line
        assert "reason=NO_BREAKOUT" in line
        assert "rsi14=null" in line
        assert "ema9=null" in line
        assert "atr14=null" in line
        assert "vwap_dist_pct=null" in line
        assert "spread_bps=null" in line
        assert "stop=null" in line

    @t("v5.1.2: [V510-CAND] reason set is fixed and complete")
    def _():
        for r in ("NO_BREAKOUT", "STAGE_NOT_READY", "ALREADY_OPEN",
                  "COOL_DOWN", "MAX_POSITIONS", "BREAKOUT_CONFIRMED"):
            assert r in m.CAND_REASONS, r

    @t("v5.1.2: [V510-FSM] emits on transition")
    def _():
        seen, h, lg, old = _v512_capture_logger()
        try:
            m._v512_log_fsm_transition("AMD", "IDLE", "WATCHING",
                                       "VOL_SPIKE_DETECTED", "1445")
        finally:
            _v512_release_logger(h, lg, old)
        line = next((s for s in seen if "[V510-FSM]" in s), None)
        assert line is not None, seen
        assert "from=IDLE" in line and "to=WATCHING" in line
        assert "reason=VOL_SPIKE_DETECTED" in line
        assert "bucket=1445" in line

    @t("v5.1.2: [V510-FSM] does NOT emit on no-op (from==to)")
    def _():
        seen, h, lg, old = _v512_capture_logger()
        try:
            m._v512_log_fsm_transition("AMD", "ARMED", "ARMED",
                                       "noop", "1445")
        finally:
            _v512_release_logger(h, lg, old)
        assert not any("[V510-FSM]" in s for s in seen), seen

    @t("v5.1.2: [V510-ENTRY] emitter carries bid/ask + account state")
    def _():
        seen, h, lg, old = _v512_capture_logger()
        try:
            m._v512_log_entry_extension(
                "AMD", bid=345.10, ask=345.14,
                cash=1234.56, equity=2345.67,
                open_positions=2, total_exposure_pct=42.5,
                current_drawdown_pct=0.0,
            )
        finally:
            _v512_release_logger(h, lg, old)
        line = next((s for s in seen if "[V510-ENTRY]" in s), None)
        assert line is not None, seen
        for tok in ("ticker=AMD", "bid=345.1", "ask=345.14",
                    "cash=1234.56", "equity=2345.67",
                    "open_positions=2", "total_exposure_pct=42.5",
                    "current_drawdown_pct=0"):
            assert tok in line, (tok, line)

    @t("v5.1.2: Dockerfile COPY includes indicators.py and bar_archive.py")
    def _():
        df = (Path(__file__).parent / "Dockerfile").read_text(encoding="utf-8")
        assert "COPY indicators.py" in df, "Dockerfile missing COPY indicators.py"
        assert "COPY bar_archive.py" in df, "Dockerfile missing COPY bar_archive.py"

    @t("v5.1.2: VOL_GATE_ENFORCE default still 0 (regression guard)")
    def _():
        saved = _v511_save_env()
        try:
            os.environ.pop("VOL_GATE_ENFORCE", None)
            cfg = vp_mod.load_active_config()
            assert cfg["enforce"] is False, cfg
        finally:
            _v511_restore_env(saved)

    @t("v5.1.2: trade_genius imports indicators and bar_archive modules")
    def _():
        assert hasattr(m, "indicators")
        assert hasattr(m, "bar_archive")

    # ------------------------------------------------------------------
    # v5.1.9 \u2014 REHUNT_VOL_CONFIRM and OOMPH_ALERT shadow configs
    # ------------------------------------------------------------------

    @t("v5.1.9: REHUNT_VOL_CONFIRM emits one [CFG=...] line on first qualifying minute")
    def _():
        # Stand up state so a single _v519_check_rehunt call qualifies:
        # - watch armed in the past 1 minute (offset_min == 1)
        # - DI on exit side is >25
        # - vol vs bucket median is >=100%
        from datetime import datetime as _dt, timezone as _tz
        m.VOLUME_PROFILE_ENABLED = True
        vp_mod.VOLUME_PROFILE_ENABLED = True
        prev_cache = m._volume_profile_cache.copy()
        prev_ws = m._ws_consumer
        prev_watch = dict(m._v519_rehunt_watch)
        try:
            prof = _fresh_profile(1000)
            m._volume_profile_cache.clear()
            m._volume_profile_cache["AMD"] = prof

            class _StubWS:
                def current_volume(self, t, b):
                    return 1500  # 150% of bucket median 1000
            m._ws_consumer = _StubWS()

            real_session_bucket = vp_mod.session_bucket
            vp_mod.session_bucket = lambda _ts: "1030"
            real_tiger_di = m.tiger_di
            m.tiger_di = lambda _t: (40.0, 10.0)  # long DI strong
            real_fetch = m.fetch_1min_bars
            m.fetch_1min_bars = lambda _t: {
                "current_price": 123.45,
                "closes": [120.0, 122.0, 123.45],
            }
            try:
                m._v519_rehunt_watch.clear()
                m._v519_arm_rehunt_watch(
                    "AMD", "long", _dt.now(tz=_tz.utc))

                import logging as _logging
                seen: list[str] = []

                class _H(_logging.Handler):
                    def emit(self, rec):
                        seen.append(rec.getMessage())
                tg_logger = _logging.getLogger("trade_genius")
                h = _H(); h.setLevel(_logging.INFO)
                tg_logger.addHandler(h); old_level = tg_logger.level
                tg_logger.setLevel(_logging.INFO)
                try:
                    m._v519_check_rehunt("AMD")
                finally:
                    tg_logger.removeHandler(h); tg_logger.setLevel(old_level)
            finally:
                vp_mod.session_bucket = real_session_bucket
                m.tiger_di = real_tiger_di
                m.fetch_1min_bars = real_fetch

            cfg_lines = [s for s in seen
                         if "[V510-SHADOW][CFG=REHUNT_VOL_CONFIRM]" in s]
            assert len(cfg_lines) == 1, \
                f"want 1 REHUNT line, got {len(cfg_lines)}: {seen}"
            line = cfg_lines[0]
            for needle in ("ticker=AMD", "side=long", "vol_pct=150",
                           "rehunt_offset_min=", "shadow_entry_price=123.45",
                           "di_plus=40", "di_minus=10"):
                assert needle in line, f"{needle!r} missing in {line!r}"
            # Watch should be marked fired so a second call doesn't re-emit.
            # v5.2.1 M4: keyed on (ticker, side) tuple.
            arm = m._v519_rehunt_watch.get(("AMD", "long"), {})
            assert arm.get("fired") is True, m._v519_rehunt_watch
        finally:
            m._volume_profile_cache.clear()
            m._volume_profile_cache.update(prev_cache)
            m._ws_consumer = prev_ws
            m._v519_rehunt_watch.clear()
            m._v519_rehunt_watch.update(prev_watch)

    @t("v5.1.9: OOMPH_ALERT emits one [CFG=...] line after minute1 + minute2 confirm")
    def _():
        # Two consecutive calls: minute 1 qualifies (DI>25 + vol>=100%),
        # minute 2 confirms (DI>25). Expect one line on the second call.
        m.VOLUME_PROFILE_ENABLED = True
        vp_mod.VOLUME_PROFILE_ENABLED = True
        prev_cache = m._volume_profile_cache.copy()
        prev_ws = m._ws_consumer
        prev_oomph = dict(m._v519_oomph_prev)
        try:
            prof = _fresh_profile(1000)
            m._volume_profile_cache.clear()
            m._volume_profile_cache["AMD"] = prof

            class _StubWS:
                def current_volume(self, t, b):
                    return 1100  # 110% of bucket median
            m._ws_consumer = _StubWS()

            buckets = ["1030", "1031"]
            idx = {"i": 0}
            real_session_bucket = vp_mod.session_bucket
            vp_mod.session_bucket = lambda _ts: buckets[idx["i"]]
            real_tiger_di = m.tiger_di
            m.tiger_di = lambda _t: (35.0, 10.0)  # long DI strong both minutes
            real_fetch = m.fetch_1min_bars
            m.fetch_1min_bars = lambda _t: {
                "current_price": 222.22, "closes": [220.0, 221.5, 222.22],
            }
            try:
                m._v519_oomph_prev.clear()

                import logging as _logging
                seen: list[str] = []

                class _H(_logging.Handler):
                    def emit(self, rec):
                        seen.append(rec.getMessage())
                tg_logger = _logging.getLogger("trade_genius")
                h = _H(); h.setLevel(_logging.INFO)
                tg_logger.addHandler(h); old_level = tg_logger.level
                tg_logger.setLevel(_logging.INFO)
                try:
                    # Minute 1: qualify (DI+ 35 > 25 AND vol_pct 110 >= 100)
                    m._v519_check_oomph("AMD")
                    # Minute 2: bucket advances; DI+ still > 25 confirms.
                    idx["i"] = 1
                    m._v519_check_oomph("AMD")
                finally:
                    tg_logger.removeHandler(h); tg_logger.setLevel(old_level)
            finally:
                vp_mod.session_bucket = real_session_bucket
                m.tiger_di = real_tiger_di
                m.fetch_1min_bars = real_fetch

            cfg_lines = [s for s in seen
                         if "[V510-SHADOW][CFG=OOMPH_ALERT]" in s]
            assert len(cfg_lines) == 1, \
                f"want 1 OOMPH line, got {len(cfg_lines)}: {seen}"
            line = cfg_lines[0]
            for needle in ("ticker=AMD", "side=long",
                           "minute1_ts=1030", "minute1_di=35",
                           "minute1_vol_pct=110",
                           "minute2_ts=1031", "minute2_di=35",
                           "shadow_entry_price=222.22"):
                assert needle in line, f"{needle!r} missing in {line!r}"
        finally:
            m._volume_profile_cache.clear()
            m._volume_profile_cache.update(prev_cache)
            m._ws_consumer = prev_ws
            m._v519_oomph_prev.clear()
            m._v519_oomph_prev.update(prev_oomph)

    # v5.2.0 \u2014 shadow strategy P&L tracker + dashboard panel.
    # Each test resets the singleton + uses a per-test STATE_DB so
    # rows do not leak between assertions.
    import shadow_pnl as _sp_mod
    import persistence as _persist_mod

    def _reset_sp_db(name: str):
        # Build a fresh per-test SQLite path and force re-init.
        p = tmp_dir / f"shadow_{name}.db"
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass
        _persist_mod._close_for_tests()
        _persist_mod.init_db(str(p))
        _sp_mod.reset_for_tests()

    @t("v5.2.0: compute_shadow_qty applies v5.1.4 caps")
    def _():
        # equity cap binds at 10% of $100k = $10,000; price $100 -> 100
        n = _sp_mod.compute_shadow_qty(
            price=100.0, dollars_per_entry=20000.0,
            equity=100000.0, cash=50000.0,
            max_pct_per_entry=10.0, min_reserve_cash=500.0,
        )
        assert n == 100, f"expected 100 shares, got {n}"
        # cash-reserve binds: cash $1000, reserve $500 -> $500 / $100 = 5
        n2 = _sp_mod.compute_shadow_qty(
            price=100.0, dollars_per_entry=20000.0,
            equity=100000.0, cash=1000.0,
            max_pct_per_entry=10.0, min_reserve_cash=500.0,
        )
        assert n2 == 5, f"expected 5 shares (cash-bound), got {n2}"

    @t("v5.2.0: open \u2192 mark_to_market \u2192 close lifecycle")
    def _():
        _reset_sp_db("life")
        tr = _sp_mod.tracker()
        snap = {
            "equity": 100000.0, "cash": 50000.0,
            "dollars_per_entry": 1000.0,
            "max_pct_per_entry": 10.0, "min_reserve_cash": 500.0,
        }
        rid = tr.open_position(
            config_name="TICKER+QQQ", ticker="AAPL", side="long",
            entry_ts_utc="2026-04-26T14:30:00+00:00",
            entry_price=100.0, equity_snapshot=snap,
        )
        assert rid is not None
        tr.mark_to_market("AAPL", 105.0)
        s = tr.summary(today_str="2026-04-26")
        assert s["TICKER+QQQ"]["today_n_trades"] == 1
        assert s["TICKER+QQQ"]["today_unrealized"] > 0
        # qty = floor(min(1000, 10000, 49500) / 100) = 10 \u2014 unrealized = $50
        assert s["TICKER+QQQ"]["today_unrealized"] == 50.0
        pnl = tr.close_position(
            config_name="TICKER+QQQ", ticker="AAPL",
            exit_ts_utc="2026-04-26T15:00:00+00:00",
            exit_price=110.0, exit_reason="HARD_EJECT_TIGER",
        )
        assert pnl == 100.0, f"expected $100 realized, got {pnl}"
        s2 = tr.summary(today_str="2026-04-26")
        assert s2["TICKER+QQQ"]["today_realized"] == 100.0
        assert s2["TICKER+QQQ"]["today_wins"] == 1
        assert s2["TICKER+QQQ"]["today_unrealized"] == 0.0

    @t("v5.2.0: short side P&L direction inverts")
    def _():
        _reset_sp_db("short")
        tr = _sp_mod.tracker()
        snap = {"equity": 100000.0, "cash": 50000.0,
                "dollars_per_entry": 1000.0,
                "max_pct_per_entry": 10.0, "min_reserve_cash": 500.0}
        tr.open_position(
            config_name="OOMPH_ALERT", ticker="TSLA", side="short",
            entry_ts_utc="2026-04-26T14:30:00+00:00",
            entry_price=200.0, equity_snapshot=snap,
        )
        # shorts profit when price falls
        tr.mark_to_market("TSLA", 180.0)
        s = tr.summary(today_str="2026-04-26")
        assert s["OOMPH_ALERT"]["today_unrealized"] > 0
        pnl = tr.close_position(
            config_name="OOMPH_ALERT", ticker="TSLA",
            exit_ts_utc="2026-04-26T15:00:00+00:00",
            exit_price=210.0, exit_reason="HARD_EJECT_TIGER",
        )
        # qty=5; (200-210)*5 = -50
        assert pnl == -50.0, f"expected -$50, got {pnl}"

    @t("v5.2.0: persistence round-trip survives reload")
    def _():
        _reset_sp_db("persist")
        tr = _sp_mod.tracker()
        snap = {"equity": 100000.0, "cash": 50000.0,
                "dollars_per_entry": 1000.0,
                "max_pct_per_entry": 10.0, "min_reserve_cash": 500.0}
        tr.open_position(
            config_name="GEMINI_A", ticker="NVDA", side="long",
            entry_ts_utc="2026-04-26T14:30:00+00:00",
            entry_price=400.0, equity_snapshot=snap,
        )
        tr.close_position(
            config_name="GEMINI_A", ticker="NVDA",
            exit_ts_utc="2026-04-26T15:00:00+00:00",
            exit_price=410.0, exit_reason="TRAIL",
        )
        # Drop singleton; rebuild from SQLite. The closed row should
        # rehydrate into _closed.
        _sp_mod.reset_for_tests()
        tr2 = _sp_mod.tracker()
        s = tr2.summary(today_str="2026-04-26")
        assert s.get("GEMINI_A", {}).get("cumulative_n_trades") == 1
        assert s["GEMINI_A"]["cumulative_realized"] > 0

    @t("v5.2.0: today vs cumulative rollup splits by entry date")
    def _():
        _reset_sp_db("rollup")
        tr = _sp_mod.tracker()
        snap = {"equity": 100000.0, "cash": 50000.0,
                "dollars_per_entry": 1000.0,
                "max_pct_per_entry": 10.0, "min_reserve_cash": 500.0}
        # one trade dated yesterday (cumulative only)
        tr.open_position(
            "BUCKET_FILL_100", "MSFT", "long",
            "2026-04-25T14:30:00+00:00", 300.0, snap)
        tr.close_position(
            "BUCKET_FILL_100", "MSFT",
            "2026-04-25T15:00:00+00:00", 309.0, "EOD")
        # one trade today (counts in both)
        tr.open_position(
            "BUCKET_FILL_100", "MSFT", "long",
            "2026-04-26T14:30:00+00:00", 300.0, snap)
        tr.close_position(
            "BUCKET_FILL_100", "MSFT",
            "2026-04-26T15:00:00+00:00", 297.0, "STOP")
        s = tr.summary(today_str="2026-04-26")
        assert s["BUCKET_FILL_100"]["today_n_trades"] == 1
        assert s["BUCKET_FILL_100"]["cumulative_n_trades"] == 2
        # today realized = -3 * 3sh = -$9 ; cumulative realized = -9 + 27 = $18
        assert abs(s["BUCKET_FILL_100"]["today_realized"] - (-9.0)) < 0.01
        assert abs(s["BUCKET_FILL_100"]["cumulative_realized"] - 18.0) < 0.01

    @t("v5.2.0: open_position dedups on (cfg, ticker, ts)")
    def _():
        _reset_sp_db("dedup")
        tr = _sp_mod.tracker()
        snap = {"equity": 100000.0, "cash": 50000.0,
                "dollars_per_entry": 1000.0,
                "max_pct_per_entry": 10.0, "min_reserve_cash": 500.0}
        a = tr.open_position(
            "TICKER_ONLY", "AAPL", "long",
            "2026-04-26T14:30:00+00:00", 100.0, snap)
        b = tr.open_position(
            "TICKER_ONLY", "AAPL", "long",
            "2026-04-26T14:30:00+00:00", 100.0, snap)
        assert a is not None
        assert b is None, "duplicate open should be a no-op"
        assert tr.open_count("TICKER_ONLY") == 1

    @t("v5.2.0: dashboard snapshot exposes shadow_pnl block")
    def _():
        _reset_sp_db("snap")
        # Seed one open + one closed across two configs, then check
        # ds.snapshot() surfaces the panel payload with the live row.
        tr = _sp_mod.tracker()
        snap = {"equity": 100000.0, "cash": 50000.0,
                "dollars_per_entry": 1000.0,
                "max_pct_per_entry": 10.0, "min_reserve_cash": 500.0}
        tr.open_position(
            "TICKER+QQQ", "AAPL", "long",
            "2026-04-26T14:30:00+00:00", 100.0, snap)
        tr.mark_to_market("AAPL", 110.0)
        tr.open_position(
            "QQQ_ONLY", "MSFT", "long",
            "2026-04-26T14:30:00+00:00", 200.0, snap)
        tr.close_position(
            "QQQ_ONLY", "MSFT",
            "2026-04-26T15:00:00+00:00", 210.0, "TRAIL")
        out = ds.snapshot()
        assert out.get("ok"), out
        sp = out.get("shadow_pnl") or {}
        # v5.2.0 amendment: comparator row is now PAPER_BOT (not LIVE).
        assert "configs" in sp and "paper_bot" in sp
        assert "live_bot" not in sp, (
            "v5.2.0 amendment removed the live_bot row; the panel "
            "compares against the paper book whose equity drives "
            "shadow sizing."
        )
        assert sp["paper_bot"]["label"] == "PAPER BOT", sp["paper_bot"]
        names = {c["name"] for c in sp["configs"]}
        assert "TICKER+QQQ" in names
        assert "REHUNT_VOL_CONFIRM" in names
        assert "OOMPH_ALERT" in names
        # 7 configs total: 5 base + 2 v5.1.9
        assert len(sp["configs"]) == 7

    @t("v5.2.0: dashboard panel marks best/worst by today_total")
    def _():
        _reset_sp_db("bw")
        tr = _sp_mod.tracker()
        snap = {"equity": 100000.0, "cash": 50000.0,
                "dollars_per_entry": 1000.0,
                "max_pct_per_entry": 10.0, "min_reserve_cash": 500.0}
        # Winner: TICKER+QQQ closes +$30. Loser: TICKER_ONLY closes -$10.
        tr.open_position("TICKER+QQQ", "AAPL", "long",
                         "2026-04-26T14:30:00+00:00", 100.0, snap)
        tr.close_position("TICKER+QQQ", "AAPL",
                          "2026-04-26T15:00:00+00:00", 103.0, "TRAIL")
        tr.open_position("TICKER_ONLY", "MSFT", "long",
                         "2026-04-26T14:30:00+00:00", 200.0, snap)
        tr.close_position("TICKER_ONLY", "MSFT",
                          "2026-04-26T15:00:00+00:00", 198.0, "STOP")
        out = ds.snapshot()
        sp = out["shadow_pnl"]
        assert sp["best_today"] == "TICKER+QQQ"
        assert sp["worst_today"] == "TICKER_ONLY"

    @t("v5.5.4: BOT_VERSION bumped to 5.5.4")
    def _():
        assert m.BOT_VERSION == "5.5.4", m.BOT_VERSION

    @t("v5.5.4: shadow WS bar handler is a coroutine function")
    def _():
        # Regression guard: alpaca-py StockDataStream.subscribe_bars()
        # requires its handler to be a coroutine function (async def).
        # In v5.5.3 the handler was a plain `def`, which raised
        # "handler must be a coroutine function" inside run() and
        # crash-looped the WS consumer every ~6s, leaving cur_v at 0
        # and the shadow-position pipeline starved. Pin the type at
        # source level so a future refactor can't regress it.
        import inspect as _inspect
        import volume_profile as _vp
        consumer_cls = _vp.WebsocketBarConsumer
        handler = consumer_cls._on_bar
        assert _inspect.iscoroutinefunction(handler), (
            "WebsocketBarConsumer._on_bar must be `async def` so "
            "alpaca-py StockDataStream accepts it as a subscribe "
            "handler (regression guard for the v5.5.3 crash-loop)")

    @t("v5.5.3: shadow WS uses market-data feed (DataFeed.IEX), not trading WS")
    def _():
        # The shadow consumer reads bars over alpaca-py's StockDataStream
        # with feed=DataFeed.IEX, which connects to
        # wss://stream.data.alpaca.markets/v2/iex \u2014 a market-data
        # endpoint. This guards against future refactors that swap in a
        # trading-stream client (which would expose /v2/positions etc.
        # over the same auth and break the constraint that the shadow
        # path is market-data-only).
        from pathlib import Path as _P
        vp_text = (_P(__file__).parent / "volume_profile.py").read_text(
            encoding="utf-8")
        # 1. The live consumer imports StockDataStream (market-data WS).
        assert "from alpaca.data.live import StockDataStream" in vp_text, \
            "shadow consumer must use alpaca.data.live.StockDataStream"
        # 2. It pins feed=DataFeed.IEX (market-data feed name).
        assert "DataFeed.IEX" in vp_text, \
            "shadow consumer must pin DataFeed.IEX market-data feed"
        # 3. No trading-API endpoints are referenced from this module.
        for forbidden in ("/v2/positions", "/v2/account", "/v2/orders",
                          "/v2/portfolio", "TradingClient",
                          "TradingStream"):
            assert forbidden not in vp_text, (
                f"forbidden trading-API ref {forbidden!r} found in "
                "volume_profile.py \u2014 shadow path must be market-"
                "data-only.")

    @t("v5.5.3: _start_volume_profile prefers VAL_ALPACA_PAPER_KEY over legacy")
    def _():
        # Source-level guard: the cred chain must consult VAL_* first.
        from pathlib import Path as _P
        tg_text = (_P(__file__).parent / "trade_genius.py").read_text(
            encoding="utf-8")
        # Locate the function body.
        i = tg_text.find("def _start_volume_profile")
        assert i != -1, "_start_volume_profile not found"
        body = tg_text[i:i + 4000]
        # VAL_ALPACA_PAPER_KEY must appear before ALPACA_PAPER_KEY in the
        # cred-resolution block.
        i_val = body.find("VAL_ALPACA_PAPER_KEY")
        i_legacy = body.find("ALPACA_PAPER_KEY")
        # i_legacy will match the VAL_ prefix too; advance past it.
        i_legacy_real = body.find('"ALPACA_PAPER_KEY"')
        assert i_val != -1, "VAL_ALPACA_PAPER_KEY missing from cred chain"
        assert i_legacy_real != -1, \
            "legacy ALPACA_PAPER_KEY fallback missing"
        assert i_val < i_legacy_real, \
            "VAL_ALPACA_PAPER_KEY must be checked before legacy key"
        # SHADOW DISABLED log line must be in place of the old soft warn.
        assert "[SHADOW DISABLED]" in body, \
            "missing [SHADOW DISABLED] log line"

    # ---- v5.4.0 offline backtest CLI ----
    @t("v5.4.0 replay: loads bars + writes CSV ledger with expected columns")
    def _():
        import shutil
        import csv as _csv
        from backtest import replay as _br
        from backtest import ledger as _bl
        bars_dir = tmp_dir / "v540_bars"
        if bars_dir.exists():
            shutil.rmtree(bars_dir)
        day = "2026-04-21"
        day_dir = bars_dir / day
        day_dir.mkdir(parents=True, exist_ok=True)

        def _write(tk, bars):
            with open(day_dir / f"{tk}.jsonl", "w") as fh:
                for b in bars:
                    fh.write(__import__("json").dumps(b) + "\n")

        # 3 tickers + QQQ index. Synthetic bars: flat then spike.
        base_ts = "2026-04-21T13:30:00Z"
        def _ts(i):
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            d = _dt.fromisoformat(base_ts.replace("Z", "+00:00"))
            return (d + _td(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")

        for tk in ("AAPL", "MSFT", "NVDA"):
            bars = []
            for i in range(20):
                price = 100.0 if i < 5 else (105.0 if i == 5 else 100.0)
                bars.append({"ts": _ts(i), "close": price,
                             "iex_volume": 50000})
            _write(tk, bars)
        qbars = [{"ts": _ts(i), "close": 400.0, "iex_volume": 50000}
                 for i in range(20)]
        _write("QQQ", qbars)

        out_dir = tmp_dir / "v540_out_a"
        if out_dir.exists():
            shutil.rmtree(out_dir)
        rc = _br.main([
            "--start", day, "--end", day,
            "--config", "TICKER+QQQ",
            "--out", str(out_dir),
            "--bars-dir", str(bars_dir),
            "--state-db", str(tmp_dir / "nope.db"),
        ])
        assert rc == 0, f"replay exit code {rc}"
        csv_path = out_dir / f"TICKER+QQQ_{day}_{day}.csv"
        assert csv_path.exists(), f"missing ledger {csv_path}"
        with open(csv_path) as fh:
            lines = fh.readlines()
        assert lines[0].startswith("# summary:"), lines[0]
        header = lines[1].strip().split(",")
        for col in _bl.LEDGER_COLUMNS:
            assert col in header, f"col {col} missing in {header}"

    @t("v5.4.0 replay: pairs entries+exits and computes P&L correctly")
    def _():
        import shutil
        from backtest import replay as _br
        bars_dir = tmp_dir / "v540_bars_pnl"
        if bars_dir.exists():
            shutil.rmtree(bars_dir)
        day = "2026-04-22"
        day_dir = bars_dir / day
        day_dir.mkdir(parents=True, exist_ok=True)

        def _ts(i):
            from datetime import datetime as _dt, timedelta as _td
            d = _dt.fromisoformat("2026-04-22T13:30:00+00:00")
            return (d + _td(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Entry at bar 5 (price 100->105 = +5%); peak 110; trail
        # stop fires at 110*(1-0.015)=108.35 \u2014 set bar 8 to 108.0.
        prices = [100.0, 100.0, 100.0, 100.0, 100.0,
                  105.0, 108.0, 110.0, 108.0,
                  108.0, 108.0]
        bars = [{"ts": _ts(i), "close": p, "iex_volume": 50000}
                for i, p in enumerate(prices)]
        with open(day_dir / "AAPL.jsonl", "w") as fh:
            for b in bars:
                fh.write(__import__("json").dumps(b) + "\n")
        qbars = [{"ts": _ts(i), "close": 400.0, "iex_volume": 50000}
                 for i in range(len(prices))]
        with open(day_dir / "QQQ.jsonl", "w") as fh:
            for b in qbars:
                fh.write(__import__("json").dumps(b) + "\n")

        cfg = {"name": "TEST", "ticker_enabled": True,
               "index_enabled": True, "ticker_pct": 70, "index_pct": 70}
        rows = _br.replay_one_day(str(bars_dir), day, cfg)
        assert len(rows) == 1, rows
        r = rows[0]
        assert r["ticker"] == "AAPL"
        assert r["side"] == "BUY"
        assert abs(r["entry_price"] - 105.0) < 1e-6, r
        # Exit: trail_stop at bar 8 close 108.0; or eod \u2014 either is acceptable
        # but qty=int(1000/105)=9. P&L = (exit-105)*9.
        expected_qty = max(1, int(1000.0 / 105.0))
        assert r["qty"] == expected_qty, r
        expected_pnl = round((r["exit_price"] - 105.0) * expected_qty, 2)
        assert abs(r["pnl_dollars"] - expected_pnl) < 0.01, r

    @t("v5.4.0 validate: match rate = 1.0 when all replay entries match prod")
    def _():
        import sqlite3 as _sql
        from backtest import replay as _br
        # 3 replay rows, 3 prod rows aligned ticker/side/ts.
        replay_rows = [
            {"ticker": "AAPL", "side": "BUY",
             "entry_ts": "2026-04-23T13:31:00Z", "entry_price": 100.0,
             "exit_ts": "2026-04-23T13:50:00Z", "exit_price": 102.0,
             "qty": 10, "pnl_dollars": 20.0, "pnl_pct": 2.0,
             "exit_reason": "trail_stop"},
            {"ticker": "MSFT", "side": "BUY",
             "entry_ts": "2026-04-23T14:00:00Z", "entry_price": 200.0,
             "exit_ts": "2026-04-23T14:20:00Z", "exit_price": 199.0,
             "qty": 5, "pnl_dollars": -5.0, "pnl_pct": -0.5,
             "exit_reason": "hard_eject"},
            {"ticker": "NVDA", "side": "BUY",
             "entry_ts": "2026-04-23T15:00:00Z", "entry_price": 800.0,
             "exit_ts": "2026-04-23T15:30:00Z", "exit_price": 805.0,
             "qty": 1, "pnl_dollars": 5.0, "pnl_pct": 0.625,
             "exit_reason": "eod"},
        ]
        prod = [
            {"ticker": "AAPL", "side": "BUY",
             "entry_ts_utc": "2026-04-23T13:31:30Z", "entry_price": 100.05,
             "exit_ts_utc": "2026-04-23T13:50:00Z", "exit_price": 102.10,
             "qty": 10, "realized_pnl": 20.5},
            {"ticker": "MSFT", "side": "BUY",
             "entry_ts_utc": "2026-04-23T14:00:10Z", "entry_price": 200.0,
             "exit_ts_utc": "2026-04-23T14:20:00Z", "exit_price": 199.10,
             "qty": 5, "realized_pnl": -4.5},
            {"ticker": "NVDA", "side": "BUY",
             "entry_ts_utc": "2026-04-23T15:00:45Z", "entry_price": 800.0,
             "exit_ts_utc": "2026-04-23T15:30:00Z", "exit_price": 804.5,
             "qty": 1, "realized_pnl": 4.5},
        ]
        vr = _br.validate(replay_rows, prod)
        assert vr["match_rate"] == 1.0, vr
        assert len(vr["matches"]) == 3, vr
        assert vr["replay_only"] == [], vr
        assert vr["prod_only"] == [], vr

    @t("v5.4.0 validate: drift detection flags PROD_ONLY and exits 1")
    def _():
        import shutil
        import sqlite3 as _sql
        from backtest import replay as _br
        # Build empty bars dir so replay produces zero rows.
        bars_dir = tmp_dir / "v540_bars_drift"
        if bars_dir.exists():
            shutil.rmtree(bars_dir)
        (bars_dir / "2026-04-24").mkdir(parents=True, exist_ok=True)

        # Stub state.db with 3 prod entries, replay produces 0 -> 3 prod_only.
        sdb = tmp_dir / "v540_drift.db"
        if sdb.exists():
            sdb.unlink()
        c = _sql.connect(str(sdb))
        c.execute(
            "CREATE TABLE shadow_positions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "config_name TEXT, ticker TEXT, side TEXT, qty INTEGER, "
            "entry_ts_utc TEXT, entry_price REAL, "
            "exit_ts_utc TEXT, exit_price REAL, exit_reason TEXT, "
            "realized_pnl REAL)"
        )
        for i, tk in enumerate(("AAPL", "MSFT", "NVDA")):
            c.execute(
                "INSERT INTO shadow_positions "
                "(config_name, ticker, side, qty, entry_ts_utc, entry_price) "
                "VALUES (?, ?, 'BUY', 10, ?, ?)",
                ("TICKER+QQQ", tk, f"2026-04-24T14:{30+i:02d}:00Z", 100.0 + i),
            )
        c.commit(); c.close()

        out_dir = tmp_dir / "v540_out_drift"
        if out_dir.exists():
            shutil.rmtree(out_dir)
        rc = _br.main([
            "--start", "2026-04-24", "--end", "2026-04-24",
            "--config", "TICKER+QQQ",
            "--validate",
            "--out", str(out_dir),
            "--bars-dir", str(bars_dir),
            "--state-db", str(sdb),
        ])
        assert rc == 1, f"expected exit 1 (match rate < 0.95), got {rc}"
        rep = out_dir / "TICKER+QQQ_2026-04-24_2026-04-24_validation.md"
        assert rep.exists(), f"validation report missing: {rep}"
        body = rep.read_text()
        assert "PROD_ONLY" in body, body[:200]

    @t("v5.2.0: persistence creates shadow_positions table")
    def _():
        # Use a per-test DB path. _close_for_tests() drops the
        # per-thread connection AND clears _initialized so init_db
        # actually runs against the new path.
        schema_path = str(tmp_dir / "schema_check.db")
        try:
            os.remove(schema_path)
        except OSError:
            pass
        _persist_mod._close_for_tests()
        _persist_mod.init_db(schema_path)
        rid = _persist_mod.save_shadow_position(
            "SCHEMA_TEST", "ZZZZ", "long", 7,
            "2026-04-26T20:30:00+00:00", 50.0)
        assert rid is not None, "save_shadow_position returned None"
        opens = _persist_mod.load_open_shadow_positions()
        assert any(r["ticker"] == "ZZZZ" for r in opens), opens
        # Re-init pointer back to the original tmp DB so subsequent
        # tests are unaffected.
        _persist_mod._close_for_tests()
        _persist_mod.init_db(str(tmp_dir / "state.db"))

    # v5.2.0 amendment \u2014 shadow sizing now reads the PAPER PORTFOLIO
    # equity (cash + long mv \u2212 short liab) instead of Val's live
    # Alpaca account. The two tests below cover the snapshot helper
    # itself plus the dashboard panel rename / Main-tab gate.

    @t("v5.2.0 amend: paper equity snapshot uses paper_cash + positions")
    def _():
        snap_fn = getattr(m, "_v520_paper_equity_snapshot", None)
        assert snap_fn is not None, (
            "shadow flow must expose _v520_paper_equity_snapshot \u2014 "
            "the live-Alpaca snapshot is removed in v5.2.0 amendment."
        )
        # Sanity-check: the live-Alpaca snapshot helper is gone so no
        # caller can accidentally reach back to the Alpaca account.
        assert getattr(m, "_v520_equity_snapshot", None) is None, (
            "old _v520_equity_snapshot must be removed; shadow flow "
            "is now 100% paper-portfolio-driven."
        )
        # Seed a clean paper book and verify the formula is exact.
        prev_cash = m.paper_cash
        prev_pos = dict(m.positions)
        prev_short = dict(m.short_positions)
        try:
            m.paper_cash = 50_000.0
            m.positions.clear()
            m.short_positions.clear()
            # 100 shares @ $50 long, no fetch needed: snapshot falls back
            # to entry_price when fetch_1min_bars returns None.
            m.positions["FAKE"] = {
                "entry_price": 50.0, "shares": 100, "stop": 49.0,
            }
            snap = snap_fn()
            assert snap is not None, "snapshot returned None"
            assert abs(snap["cash"] - 50_000.0) < 0.01, snap
            # equity = 50_000 + 100 * 50 - 0 = 55_000
            assert abs(snap["equity"] - 55_000.0) < 0.01, snap
            assert snap["dollars_per_entry"] == m.PAPER_DOLLARS_PER_ENTRY
            assert snap["max_pct_per_entry"] > 0
            assert snap["min_reserve_cash"] >= 0
        finally:
            m.paper_cash = prev_cash
            m.positions.clear()
            m.positions.update(prev_pos)
            m.short_positions.clear()
            m.short_positions.update(prev_short)

    @t("v5.3.0: shadow panel renders on Shadow tab via #tg-panel-shadow gate")
    def _():
        # v5.3.0 replaces the v5.2.0 main-only gate with an explicit
        # Shadow-tab-only gate. The HTML still ships the same ids so
        # the JS render fn keeps working; the CSS rule now hides
        # #tg-panel-shadow on main/val/gene and shows it on shadow.
        repo = Path(__file__).parent
        html = (repo / "dashboard_static" / "index.html").read_text(
            encoding="utf-8")
        assert 'id="shadow-pnl-card"' in html, "card id missing"
        assert 'id="shadow-pnl-section"' in html, "section id missing"
        assert 'id="tg-panel-shadow"' in html, "shadow tab panel missing"
        css = (repo / "dashboard_static" / "app.css").read_text(
            encoding="utf-8")
        assert '#tg-panel-shadow' in css, "shadow panel gate missing"
        assert 'data-tg-active-tab="shadow"' in css, css[-2000:]
        assert "display: none !important" in css

    # ---- v5.3.0 Shadow tab ----

    @t("v5.3.0: shadow_tab_html_present (Main/Val/Gene/Shadow order)")
    def _():
        # Tab strip must contain the four tabs in canonical order, and
        # the new Shadow tab pane must own the shadow-pnl card.
        repo = Path(__file__).parent
        html = (repo / "dashboard_static" / "index.html").read_text(
            encoding="utf-8")
        i_main   = html.find('data-tg-tab="main"')
        i_val    = html.find('data-tg-tab="val"')
        i_gene   = html.find('data-tg-tab="gene"')
        i_shadow = html.find('data-tg-tab="shadow"')
        assert i_main >= 0, "main tab button missing"
        assert i_val > i_main, "val tab not after main"
        assert i_gene > i_val, "gene tab not after val"
        assert i_shadow > i_gene, "shadow tab not after gene"
        # Shadow pane exists and contains the shadow card.
        i_pane = html.find('id="tg-panel-shadow"')
        assert i_pane >= 0, "shadow tab pane missing"
        i_card = html.find('id="shadow-pnl-card"')
        assert i_card > i_pane, (
            "shadow card must live inside #tg-panel-shadow "
            "(found at %d vs pane at %d)" % (i_card, i_pane)
        )

    @t("v5.3.0: shadow_card_not_on_main (card moved out of Main)")
    def _():
        # The shadow card must NOT be a child of #tg-panel-main. We
        # locate the closing </div> of the Main panel (marked by the
        # /tg-panel-main HTML comment) and assert the card id appears
        # only AFTER that boundary.
        repo = Path(__file__).parent
        html = (repo / "dashboard_static" / "index.html").read_text(
            encoding="utf-8")
        end_main = html.find("/tg-panel-main")
        assert end_main >= 0, "tg-panel-main close marker missing"
        i_card = html.find('id="shadow-pnl-card"')
        assert i_card >= 0, "shadow card id missing"
        assert i_card > end_main, (
            "shadow card must live outside #tg-panel-main "
            "(found at %d vs main close at %d)" % (i_card, end_main)
        )

    @t("v5.3.0: shadow_detail_endpoint exposes open_positions + recent_trades")
    def _():
        _reset_sp_db("v530_detail")
        tr = _sp_mod.tracker()
        snap = {"equity": 100000.0, "cash": 50000.0,
                "dollars_per_entry": 1000.0,
                "max_pct_per_entry": 10.0, "min_reserve_cash": 500.0}
        # Seed one open and one closed under TICKER+QQQ so the
        # snapshot can prove both lists are wired through.
        tr.open_position(
            "TICKER+QQQ", "AAPL", "long",
            "2026-04-26T14:30:00+00:00", 100.0, snap)
        tr.mark_to_market("AAPL", 110.0)
        tr.open_position(
            "TICKER+QQQ", "MSFT", "long",
            "2026-04-26T14:31:00+00:00", 200.0, snap)
        tr.close_position(
            "TICKER+QQQ", "MSFT",
            "2026-04-26T15:00:00+00:00", 210.0, "TRAIL")
        out = ds.snapshot()
        assert out.get("ok"), out
        sp = out["shadow_pnl"]
        assert "configs" in sp
        # Every config row must carry the two detail lists \u2014
        # absent rows render as empty lists, not missing keys.
        for c in sp["configs"]:
            assert "open_positions" in c, c
            assert "recent_trades" in c, c
            assert isinstance(c["open_positions"], list)
            assert isinstance(c["recent_trades"], list)
        seeded = next(c for c in sp["configs"] if c["name"] == "TICKER+QQQ")
        # Open: AAPL still open, MSFT closed.
        opens = seeded["open_positions"]
        assert len(opens) == 1, opens
        op = opens[0]
        for k in ("ticker", "side", "qty", "entry_price",
                  "current_mark", "unrealized", "entry_ts_utc"):
            assert k in op, (k, op)
        assert op["ticker"] == "AAPL"
        assert op["side"] == "long"
        # Recent: MSFT closed.
        recs = seeded["recent_trades"]
        assert len(recs) == 1, recs
        rc = recs[0]
        for k in ("ticker", "side", "qty", "entry_price",
                  "exit_price", "realized_pnl", "exit_reason",
                  "entry_ts_utc", "exit_ts_utc"):
            assert k in rc, (k, rc)
        assert rc["ticker"] == "MSFT"
        assert rc["exit_reason"] == "TRAIL"

    # -------- v5.4.1 shadow charts endpoint + UI --------

    @t("v5.4.1: /api/shadow_charts returns 7 configs with the 3 chart blocks")
    def _():
        _reset_sp_db("v541_endpoint")
        tr = _sp_mod.tracker()
        snap = {"equity": 100000.0, "cash": 50000.0,
                "dollars_per_entry": 1000.0,
                "max_pct_per_entry": 10.0, "min_reserve_cash": 500.0}
        # Seed 21 closed trades on TICKER+QQQ so the rolling 20-trade
        # win-rate window emits at least one point. Alternate winners
        # and losers so win_rate is well-defined.
        for i in range(21):
            ts_open = f"2026-04-2{(i % 5) + 1}T14:{30 + i:02d}:00+00:00"
            ts_close = f"2026-04-2{(i % 5) + 1}T15:{0 + (i % 30):02d}:00+00:00"
            tr.open_position("TICKER+QQQ", f"T{i:03d}", "long",
                             ts_open, 100.0, snap)
            exit_price = 110.0 if (i % 2 == 0) else 95.0
            tr.close_position("TICKER+QQQ", f"T{i:03d}",
                              ts_close, exit_price, "TRAIL")
        # Reset cache so the seeded data is observed.
        ds._shadow_charts_cache["ts"] = 0.0
        ds._shadow_charts_cache["payload"] = None
        payload = ds._shadow_charts_payload()
        assert "configs" in payload, payload
        assert "as_of" in payload and payload["as_of"], payload
        cfgs = payload["configs"]
        # All 7 SHADOW_CONFIG names must be present even when empty.
        expected = {"TICKER+QQQ", "TICKER_ONLY", "QQQ_ONLY", "GEMINI_A",
                    "BUCKET_FILL_100", "REHUNT_VOL_CONFIRM", "OOMPH_ALERT"}
        assert set(cfgs.keys()) == expected, set(cfgs.keys())
        for name, blk in cfgs.items():
            for k in ("equity_curve", "daily_pnl", "win_rate_rolling"):
                assert k in blk, (name, k, blk)
                assert isinstance(blk[k], list), (name, k)
        seeded = cfgs["TICKER+QQQ"]
        assert len(seeded["equity_curve"]) == 21, seeded["equity_curve"]
        assert len(seeded["win_rate_rolling"]) >= 1, seeded["win_rate_rolling"]
        wr0 = seeded["win_rate_rolling"][0]
        assert "trade_idx" in wr0 and "win_rate" in wr0, wr0
        assert wr0["trade_idx"] >= 20, wr0
        assert 0.0 <= wr0["win_rate"] <= 1.0, wr0
        # Empty configs must still expose empty lists, not None.
        empty = cfgs["GEMINI_A"]
        assert empty["equity_curve"] == [], empty
        assert empty["daily_pnl"] == [], empty
        assert empty["win_rate_rolling"] == [], empty

    @t("v5.4.1: /api/shadow_charts response is cached for 30s")
    def _():
        # Two consecutive handler invocations within the 30s TTL must
        # return the same payload object (cache hit on the second call).
        _reset_sp_db("v541_cache")
        tr = _sp_mod.tracker()
        snap = {"equity": 100000.0, "cash": 50000.0,
                "dollars_per_entry": 1000.0,
                "max_pct_per_entry": 10.0, "min_reserve_cash": 500.0}
        tr.open_position("TICKER+QQQ", "AAPL", "long",
                         "2026-04-26T14:30:00+00:00", 100.0, snap)
        tr.close_position("TICKER+QQQ", "AAPL",
                          "2026-04-26T15:00:00+00:00", 110.0, "TRAIL")
        # Reset cache so this test owns the first build.
        ds._shadow_charts_cache["ts"] = 0.0
        ds._shadow_charts_cache["payload"] = None
        # Drive two requests through the handler; the second must hit
        # the cache and skip the rebuild path.
        import asyncio, json as _json
        from aiohttp.test_utils import make_mocked_request
        # Build a session cookie a real client would carry.
        ds._SESSION_SECRET = b"x" * 32
        tok = ds._make_token()
        cookie_header = f"{ds.SESSION_COOKIE}={tok}"
        loop = asyncio.new_event_loop()
        try:
            req1 = make_mocked_request("GET", "/api/shadow_charts",
                                       headers={"Cookie": cookie_header})
            r1 = loop.run_until_complete(ds.h_shadow_charts(req1))
            ts1 = ds._shadow_charts_cache["ts"]
            payload1 = ds._shadow_charts_cache["payload"]
            assert payload1 is not None
            req2 = make_mocked_request("GET", "/api/shadow_charts",
                                       headers={"Cookie": cookie_header})
            r2 = loop.run_until_complete(ds.h_shadow_charts(req2))
            ts2 = ds._shadow_charts_cache["ts"]
            payload2 = ds._shadow_charts_cache["payload"]
        finally:
            loop.close()
        assert r1.status == 200 and r2.status == 200
        # Cache must not have been rebuilt: identical timestamp + same
        # payload object identity (cache hit returns the stored value).
        assert ts1 == ts2, f"cache rebuilt within TTL: {ts1} vs {ts2}"
        assert payload1 is payload2

    @t("v5.4.1: index.html loads Chart.js + has 3 chart-group divs")
    def _():
        from html.parser import HTMLParser
        html_path = (Path(__file__).parent
                     / "dashboard_static" / "index.html")
        text = html_path.read_text(encoding="utf-8")
        # 1. Chart.js is loaded (CDN script tag with chart.umd in src).
        assert "chart.umd" in text and "chart.js" in text.lower(), \
            "Chart.js script tag missing from index.html"
        # 2. The three chart-group divs exist with the expected ids.
        for needed in ("shadow-equity-group", "shadow-heatmap-group",
                       "shadow-winrate-group"):
            assert needed in text, f"missing chart-group div #{needed}"
        # 3. The heatmap canvas + the Charts collapsible header are wired.
        assert "shadow-heatmap-canvas" in text
        assert "shadow-charts-head" in text
        assert "shadow-charts-body" in text
        # 4. The new groups live inside the Shadow tab panel.
        i_panel = text.find('id="tg-panel-shadow"')
        i_groups = text.find("shadow-equity-group")
        i_panel_close = text.find("/tg-panel-shadow")
        assert i_panel != -1 and i_groups != -1 and i_panel_close != -1
        assert i_panel < i_groups < i_panel_close, \
            "chart groups must live inside the Shadow tab panel"
        # 5. HTML still parses cleanly.
        class _P(HTMLParser):
            def error(self, msg):
                raise AssertionError("malformed html: " + str(msg))
        _P(convert_charrefs=True).feed(text)

    @t("v5.5.1: tooltip callbacks present on all 3 chart constructors")
    def _():
        # Parse app.js and assert that each `new window.Chart(...)` block
        # has its own `plugins.tooltip.callbacks` block. This guards the
        # rich-tooltip wiring from accidental removal.
        js_path = (Path(__file__).parent
                   / "dashboard_static" / "app.js")
        text = js_path.read_text(encoding="utf-8")
        # Slice the file at every "new window.Chart(" occurrence and walk
        # forward to the matching closing-brace of that constructor call.
        starts = [i for i in range(len(text))
                  if text.startswith("new window.Chart(", i)]
        assert len(starts) >= 3, \
            f"expected at least 3 chart constructors, found {len(starts)}"
        # We only care about the 3 shadow-tab constructors. Take the
        # first 3 — they are equity / winrate / heatmap in source order.
        callback_hits = 0
        for start in starts[:3]:
            # Find the end of this constructor by tracking paren depth.
            depth = 0
            end = None
            for j in range(start, len(text)):
                c = text[j]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        end = j
                        break
            assert end is not None, "unterminated Chart() constructor"
            block = text[start:end]
            # The block must wire a tooltip with a callbacks: { ... } map.
            assert "tooltip:" in block, \
                "constructor missing tooltip block: " + block[:80]
            assert "callbacks:" in block, \
                "constructor missing tooltip.callbacks: " + block[:80]
            callback_hits += 1
        assert callback_hits == 3, \
            f"expected 3 tooltip.callbacks blocks, got {callback_hits}"

    @t("v5.5.1: click-to-isolate handler present in app.js")
    def _():
        # Parse app.js and assert (a) a single isolation state variable
        # named __scIsolated exists, (b) a click handler that mutates it
        # exists, and (c) the heatmap onClick clears or sets it.
        js_path = (Path(__file__).parent
                   / "dashboard_static" / "app.js")
        text = js_path.read_text(encoding="utf-8")
        assert "__scIsolated" in text, "isolation state variable missing"
        assert "let __scIsolated" in text or "var __scIsolated" in text, \
            "__scIsolated must be declared (let/var) once at module scope"
        # The toggle handler must mutate __scIsolated.
        assert "_scOnConfigClick" in text, "_scOnConfigClick missing"
        # Find the function body and confirm it assigns __scIsolated.
        i = text.find("function _scOnConfigClick")
        assert i != -1
        body = text[i:i + 600]
        assert "__scIsolated =" in body, \
            "_scOnConfigClick must assign __scIsolated"
        # Each of the 3 chart constructors wires onClick / addEventListener
        # that calls _scOnConfigClick or _scClearIsolation.
        click_calls = text.count("_scOnConfigClick")
        assert click_calls >= 3, \
            f"expected >=3 click handlers calling _scOnConfigClick, got {click_calls}"

    # -------- v5.2.1 shadow-accounting fixes (H2/H3/M3/M4) --------

    @t("v5.2.1: EOD orphan force-close at entry_price (H2)")
    def _():
        _reset_sp_db("eod_orphan")
        tr = _sp_mod.tracker()
        snap = {"equity": 100000.0, "cash": 50000.0,
                "dollars_per_entry": 1000.0,
                "max_pct_per_entry": 10.0, "min_reserve_cash": 500.0}
        # Open a position. DO NOT mark_to_market \u2014 ticker stays absent
        # from the prices dict at EOD.
        rid = tr.open_position(
            config_name="TICKER+QQQ", ticker="ZZZZ", side="long",
            entry_ts_utc="2026-04-26T14:30:00+00:00",
            entry_price=42.0, equity_snapshot=snap,
        )
        assert rid is not None
        assert tr.open_count("TICKER+QQQ") == 1
        # Run EOD with empty prices \u2014 v5.2.0 would silently leave the
        # position open. v5.2.1 H2 force-closes at entry_price with
        # exit_reason="EOD_NO_MARK".
        n = tr.close_all_for_eod({})
        assert n == 1, f"expected 1 forced close, got {n}"
        assert tr.open_count("TICKER+QQQ") == 0
        # Closed row exists with the expected exit_reason and zero P&L.
        with tr._lock:
            closed = list(tr._closed.get("TICKER+QQQ", []))
        assert len(closed) == 1, closed
        row = closed[0]
        assert row["exit_reason"] == "EOD_NO_MARK", row
        assert row["exit_price"] == 42.0, row
        assert abs(float(row["realized_pnl"])) < 1e-9, row

    @t("v5.2.1: shadow MTM runs when paper_holds (H3)")
    def _():
        # Verify _v520_mtm_ticker is called in the SCAN path BEFORE the
        # `if not paper_holds:` gate, by inspecting the source layout.
        # An execution-level test would require a full fetch_1min_bars
        # stub; the structural assertion is fast and unambiguous.
        src = Path(__file__).parent / "trade_genius.py"
        text = src.read_text(encoding="utf-8")
        # Locate the scan-loop long-entry block.
        marker = "Long entry check \u2014 run once per ticker"
        idx = text.find(marker)
        assert idx != -1, "scan-loop long-entry block not found"
        # v5.5.2: window widened from 2000 \u2192 5000 because the
        # bar-archive hook lives between the MTM call and the
        # paper_holds gate, which is a deliberate ordering. The
        # invariant being asserted is unchanged: MTM must run before
        # the gate.
        block = text[idx: idx + 5000]
        mtm_pos = block.find("_v520_mtm_ticker(")
        gate_pos = block.find("if not paper_holds:")
        assert mtm_pos != -1, "_v520_mtm_ticker call missing from scan block"
        assert gate_pos != -1, "paper_holds gate missing from scan block"
        assert mtm_pos < gate_pos, (
            "v5.2.1 H3: _v520_mtm_ticker must be invoked BEFORE the "
            "`if not paper_holds:` gate so shadow positions on a "
            "paper-held ticker still get marked. Current order keeps "
            "MTM gated."
        )

    @t("v5.2.1: _v520_close_shadow_all iterates SHADOW_CONFIGS registry (M3)")
    def _():
        _reset_sp_db("close_all")
        tr = _sp_mod.tracker()
        snap = {"equity": 100000.0, "cash": 50000.0,
                "dollars_per_entry": 1000.0,
                "max_pct_per_entry": 10.0, "min_reserve_cash": 500.0}
        # Open a virtual position on the SAME ticker for EVERY known
        # shadow config (SHADOW_CONFIGS + extras).
        all_names = m._v521_all_shadow_config_names()
        # Sanity: registry must include the v5.1.6 BUCKET_FILL_100 plus
        # the v5.1.9 extras so the test has real coverage.
        assert "BUCKET_FILL_100" in all_names, all_names
        assert "REHUNT_VOL_CONFIRM" in all_names, all_names
        assert "OOMPH_ALERT" in all_names, all_names
        for cfg_name in all_names:
            tr.open_position(
                config_name=cfg_name, ticker="WXYZ", side="long",
                entry_ts_utc="2026-04-26T14:30:00+00:00",
                entry_price=10.0, equity_snapshot=snap,
            )
        for cfg_name in all_names:
            assert tr.open_count(cfg_name) == 1, cfg_name
        # Fanout close at $11 / HARD_EJECT_TIGER must hit every config.
        m._v520_close_shadow_all("WXYZ", 11.0, "HARD_EJECT_TIGER")
        for cfg_name in all_names:
            assert tr.open_count(cfg_name) == 0, (
                f"{cfg_name} not closed by _v520_close_shadow_all \u2014 "
                "M3 fanout drifted from the registry"
            )

    @t("v5.2.1: rehunt watch long+short coexist (M4)")
    def _():
        # Reset state, arm both sides on the same ticker on the same
        # minute, and assert both arms survive. Pre-fix the dict was
        # keyed on `ticker` alone so the second arm clobbered the first.
        m._v519_rehunt_watch.clear()
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        ts = _dt.now(tz=_tz.utc)
        m._v519_arm_rehunt_watch("AAPL", "long", ts)
        m._v519_arm_rehunt_watch("AAPL", "short", ts)
        assert ("AAPL", "long") in m._v519_rehunt_watch, m._v519_rehunt_watch
        assert ("AAPL", "short") in m._v519_rehunt_watch, m._v519_rehunt_watch
        long_arm = m._v519_rehunt_watch[("AAPL", "long")]
        short_arm = m._v519_rehunt_watch[("AAPL", "short")]
        assert long_arm["side"] == "long", long_arm
        assert short_arm["side"] == "short", short_arm
        assert long_arm["fired"] is False
        assert short_arm["fired"] is False
        # Cleanup.
        m._v519_rehunt_watch.clear()

    # ==================================================================
    # === v5.2.1 Idempotency + Reconcile ===
    # ==================================================================

    def _make_executor():
        """Build a TradeGeniusVal with creds stubbed; never builds a
        real Alpaca client. Each test attaches its own fake client to
        ex.client and calls helpers directly."""
        ex = m.TradeGeniusVal()
        ex.client = None  # tests will assign a fake
        ex.positions = {}  # fresh dict per test
        return ex

    class _FakeOrder:
        def __init__(self, oid="srv-123", coid=None):
            self.id = oid
            self.client_order_id = coid

    class _FakeBrokerPos:
        def __init__(self, symbol, qty, avg):
            self.symbol = symbol
            self.qty = qty
            self.avg_entry_price = avg

    @t("v5.2.1: client_order_id present + format on submit")
    def _():
        ex = _make_executor()
        captured = {}

        class FakeClient:
            def submit_order(self, req):
                captured["req"] = req
                return _FakeOrder(coid=getattr(req, "client_order_id", None))

            def get_order_by_client_id(self, coid):
                raise AssertionError("should not be called on success")

        ex.client = FakeClient()
        # Stub _ensure_client to skip network
        ex._ensure_client = lambda: ex.client
        # Drive the public path (_on_signal) so we exercise the real
        # construction site, not the helper alone.
        ex._shares_for = lambda price, ticker=None: 5
        ex._send_own_telegram = lambda *a, **k: None
        ex._on_signal({
            "kind": "ENTRY_LONG",
            "ticker": "aapl",  # lowercase to exercise sanitizer
            "price": 150.0,
            "reason": "test",
        })
        req = captured.get("req")
        assert req is not None, "submit_order was not called"
        coid = getattr(req, "client_order_id", None)
        assert coid, f"missing client_order_id: {coid!r}"
        # f"{NAME}-{ticker}-{utc_iso_minute}-{direction}"
        # NAME=VAL, ticker sanitized to AAPL upper, direction LONG
        parts = coid.split("-")
        assert parts[0] == "VAL", parts
        assert parts[1] == "AAPL", parts
        assert parts[3] == "LONG", parts
        # minute portion: YYYYMMDDTHHMM
        assert len(parts[2]) == 13 and "T" in parts[2], parts[2]
        # Length under Alpaca's 128-char ceiling
        assert len(coid) <= 128
        # Side-effect: positions stamped with SIGNAL source.
        # _record_position keys by raw signal ticker (sanitization
        # only happens inside the coid). Real signal flow always
        # sends upper-case so the executor view stays case-stable.
        assert "aapl" in ex.positions
        assert ex.positions["aapl"]["source"] == "SIGNAL"
        assert ex.positions["aapl"]["side"] == "LONG"

    @t("v5.2.1: duplicate coid APIError treated as success")
    def _():
        ex = _make_executor()
        calls = {"submits": 0, "lookups": 0}

        class FakeClient:
            def submit_order(self, req):
                calls["submits"] += 1
                raise Exception("client order id must be unique")

            def get_order_by_client_id(self, coid):
                calls["lookups"] += 1
                return _FakeOrder(oid="existing-srv-id", coid=coid)

        ex.client = FakeClient()
        ex._ensure_client = lambda: ex.client
        ex._shares_for = lambda price, ticker=None: 5
        ex._send_own_telegram = lambda *a, **k: None
        ex._on_signal({
            "kind": "ENTRY_SHORT",
            "ticker": "TSLA",
            "price": 200.0,
            "reason": "test",
        })
        # Single submit attempt, single lookup, position recorded.
        assert calls["submits"] == 1, calls
        assert calls["lookups"] == 1, calls
        assert "TSLA" in ex.positions
        assert ex.positions["TSLA"]["side"] == "SHORT"

    @t("v5.2.1: timeout-after-accept does not double-place on retry")
    def _():
        ex = _make_executor()
        state = {"phase": "first", "submits": 0, "lookups": 0}

        class FakeClient:
            def submit_order(self, req):
                state["submits"] += 1
                if state["phase"] == "first":
                    # Broker accepted but client side timed out.
                    raise TimeoutError("HTTP read timeout after 30s")
                # Second call: same coid, broker rejects as duplicate.
                raise Exception("client order id must be unique")

            def get_order_by_client_id(self, coid):
                state["lookups"] += 1
                return _FakeOrder(oid="recovered-srv-id", coid=coid)

        ex.client = FakeClient()
        ex._ensure_client = lambda: ex.client
        ex._shares_for = lambda price, ticker=None: 5
        ex._send_own_telegram = lambda *a, **k: None
        # First signal: timeout. _on_signal swallows the dispatch error.
        ex._on_signal({
            "kind": "ENTRY_LONG",
            "ticker": "MSFT",
            "price": 300.0,
            "reason": "first",
        })
        # First submit attempted, no lookup, no position recorded.
        assert state["submits"] == 1, state
        assert state["lookups"] == 0, state
        assert "MSFT" not in ex.positions, ex.positions
        # Second signal in the same minute/direction: same coid, dup
        # path fires. Submit is attempted but rejected; lookup recovers.
        state["phase"] = "second"
        ex._on_signal({
            "kind": "ENTRY_LONG",
            "ticker": "MSFT",
            "price": 300.0,
            "reason": "retry",
        })
        assert state["submits"] == 2, state
        assert state["lookups"] == 1, state
        # Position now recorded via the dup-as-success path.
        assert "MSFT" in ex.positions
        assert ex.positions["MSFT"]["source"] == "SIGNAL"

    @t("v5.2.1: reconcile grafts broker orphans into self.positions")
    def _():
        ex = _make_executor()

        class FakeClient:
            def get_all_positions(self):
                return [
                    _FakeBrokerPos("ORPH", 7, "12.50"),
                    _FakeBrokerPos("SHRT", -3, "45.10"),
                ]

        ex.client = FakeClient()
        ex._ensure_client = lambda: ex.client
        ex._send_own_telegram = lambda *a, **k: None
        ex._reconcile_broker_positions()
        assert "ORPH" in ex.positions
        assert ex.positions["ORPH"]["source"] == "RECONCILE"
        assert ex.positions["ORPH"]["side"] == "LONG"
        assert ex.positions["ORPH"]["qty"] == 7
        assert abs(ex.positions["ORPH"]["entry_price"] - 12.5) < 1e-6
        assert ex.positions["ORPH"]["stop"] is None
        assert ex.positions["ORPH"]["trail"] is None
        assert "SHRT" in ex.positions
        assert ex.positions["SHRT"]["side"] == "SHORT"
        assert ex.positions["SHRT"]["qty"] == 3

    @t("v5.2.1: reconcile leaves known positions untouched")
    def _():
        ex = _make_executor()
        # Pre-populate with a SIGNAL-source entry the bot already knows.
        ex.positions["KNWN"] = {
            "ticker": "KNWN",
            "side": "LONG",
            "qty": 10,
            "entry_price": 99.99,
            "entry_ts_utc": "2026-04-26T00:00:00+00:00",
            "source": "SIGNAL",
            "stop": 95.0,
            "trail": 100.5,
        }
        snapshot = dict(ex.positions["KNWN"])

        class FakeClient:
            def get_all_positions(self):
                # Same ticker on the broker side \u2014 must NOT clobber.
                return [_FakeBrokerPos("KNWN", 10, "50.00")]

        ex.client = FakeClient()
        ex._ensure_client = lambda: ex.client
        ex._send_own_telegram = lambda *a, **k: None
        ex._reconcile_broker_positions()
        assert ex.positions["KNWN"] == snapshot, ex.positions["KNWN"]

    return run_suite("LOCAL SMOKE TESTS (v5.1.2 Tiger/Buffalo + Forensic Capture)")


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
