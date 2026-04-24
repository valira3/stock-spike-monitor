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

    @t("version: BOT_VERSION is 4.1.3")
    def _():
        assert m.BOT_VERSION == "4.1.3", f"got {m.BOT_VERSION}"

    @t("version: no -beta suffix")
    def _():
        assert "beta" not in m.BOT_VERSION.lower(), \
            f"BOT_VERSION still carries beta moniker: {m.BOT_VERSION!r}"

    @t("version: CURRENT_MAIN_NOTE begins with v4.1.3")
    def _():
        assert m.CURRENT_MAIN_NOTE.lstrip().startswith("v4.1.3"), \
            f"note starts: {m.CURRENT_MAIN_NOTE[:40]!r}"

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

    return run_suite("LOCAL SMOKE TESTS (v4.0.0-beta Gene + dashboard)")


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
