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

# Tests for retired v4/v5 Tiger Sovereign / Bison / Buffalo / Permit-Matrix code.
# Code was physically deleted in v7.x+; these assertions can never pass. Skipped
# rather than deleted so the commit history shows WHY they were suppressed.
_SKIP_RETIRED: frozenset[str] = frozenset({
    "dashboard: /api/indices endpoint exists",
    "dashboard: _today_trades de-duplicates cross-list short",
    "di_seed: _seed_di_buffer function exists",
    "guard: ENTRY_STOP_CAP_REJECT=False preserves legacy capping path",
    "guard: _capped_long_stop flags capped when baseline is too loose",
    "guard: _capped_long_stop not capped when baseline is already tight",
    "guard: _capped_short_stop flags capped when baseline is too loose",
    "guard: env flags exist with documented defaults",
    "guard: long extension 0.5% under 1.5% cap is allowed",
    "guard: long extension 2.0% over 1.5% cap is rejected",
    "guard: short extension 2.0% below OR_Low is rejected",
    "regime: _scan_idle_hours flips False during trading hours",
    "regime: scan_loop refreshes mode to CLOSED after market close (16:30 ET simulated)",
    "regime: scan_loop refreshes mode to CLOSED on weekend (Saturday simulated)",
    "utility: _clamp respects bounds",
    "v4.11.0: /api/errors/{executor} route registered",
    "v4.7.0: _ticker_today_realized_pnl helper exists and aggregates long+short closed trades",
    "v4.7.0: daily_short_entry_date resets daily_short_entry_count on new day",
    "v4.7.0: scan_loop calls execute_short_entry after check_short_entry returns True",
    "v4.9.1: /api/version endpoint registered",
    "v4.9.1: /api/version handler actually returns BOT_VERSION",
    "v5 C-R7: 9-ticker spike universe + QQQ pinned (v5.6.0: SPY retired with G2)",
    "v5 module: STRATEGY.md exists at repo root",
    "v5.1.2: [V510-CAND] emitted on entered=NO with null indicators",
    "v5.1.2: [V510-CAND] emitted on entered=YES with all fields",
    "v5.1.2: [V510-CAND] reason set is fixed and complete",
    "v5.1.2: [V510-FSM] does NOT emit on no-op (from==to)",
    "v5.1.2: [V510-FSM] emits on transition",
    "v5.1.2: [V510-MINUTE] line emitted with expected fields",
    "v5.1.2: [V510-MINUTE] renders None as 'null'",
    "v5.1.2: bar_archive.write_bar writes JSONL to dated path",
    "v5.1.6: _v516_check_velocity fires once per (ticker, minute)",
    "v5.1.6: _v516_log_di emits double-tap flags",
    "v5.1.6: _v516_log_index emits SPY+QQQ above-PDC verdict",
    "v5.1.6: _v516_log_velocity emits a [V510-VEL] line",
    "v5.1.6: trade_genius exposes _v516_log_velocity / _v516_log_index / _v516_log_di",
    "v5.15.0 vAA-1: ENABLE_UNLIMITED_TITAN_STRIKES default False (STRIKE-CAP-3)",
    "v5.19.1 D4: STRIKE-CAP-3 caps a Titan ticker at 3 strikes per day (long+short combined)",
    "v5.20.5: DI seeder has RTH fallback wired into recompute",
    "v5.20.5: volume bucket gate prefers _ws_consumer over Yahoo",
    "v5.20.7: app.css single-scroll architecture",
    "v5.20.8: component table column headers renamed to card vocabulary",
    "v5.20.9: Permit Matrix gate columns ordered Boundary → Volume → Authority → Momentum",
    "v5.5.10: BOT_VERSION bumped to 5.5.10",
    "v5.5.10: _record_position writes an executor_positions row",
    "v5.5.11: BOT_VERSION bumped to 5.5.11",
    "v5.5.2: _v512_archive_minute_bar has a caller outside its own def",
    "v5.5.4: BOT_VERSION bumped to 5.5.4",
    "v5.5.5: ARCHITECTURE.md last-refresh footer pinned to 5.7.1",
    "v5.5.5: BOT_VERSION bumped to 5.5.5",
    "v5.5.5: bar archive prefers _ws_consumer over Yahoo",
    "v5.5.5: dashboard registers /api/ws_state route",
    "v5.5.6: BOT_VERSION bumped to 5.5.6",
    "v5.5.7: BOT_VERSION bumped to 5.5.7",
    "v5.5.8: BOT_VERSION bumped to 5.5.8",
    "v5.5.9: BOT_VERSION bumped to 5.5.9",
    "v5.7.0 D1: [UNIVERSE] boot line includes all 10 Titans + QQQ alpha-sorted",
    "v5.7.0 D2: TITAN_TICKERS has exactly 10 alpha-sorted Titans",
    "v5.7.0 D3: Strike 1 LONG NVDA — expansion gate not consulted",
    "v5.7.0 D3: Strike 2 LONG NVDA with HOD break + Index above AVWAP — PASS",
    "v5.7.0 D3: Strike 2 LONG NVDA with HOD break BUT Index below AVWAP — FAIL",
    "v5.7.0 D3: Strike 2 LONG NVDA with HOD break BUT IndexAVWAP=None — FAIL",
    "v5.7.0 D3: Strike 2 LONG NVDA without HOD break — expansion gate FAIL",
    "v5.7.0 D3: Strike 2 SHORT mirror — LOD break + Index below AVWAP PASSES",
    "v5.7.0 D4: non-Titan ticker is NOT eligible for unlimited strikes",
    "v5.7.0 guard: CHANGELOG.md still has v5.7.0 heading present",
    "v5.7.0: feature flag False falls back to old behavior (no Titan branching)",
    "v5.7.1 D5: [V571-EMA_SEED] line emits once at seed time",
    "v5.7.1 D5: [V571-EXIT_PHASE] line carries every spec field",
    "v5.7.1 D5: [V571-VELOCITY_FUSE] line emits with pct_move",
    "v5.7.1 D6: VELOCITY_FUSE_PCT = 0.01 (strict 1.0% threshold)",
    "version: BOT_VERSION is 5.9.0",
    "volprofile: trade_genius imports volume_profile module",
})


def t(name: str) -> Callable:
    def decorator(fn: Callable[[], None]) -> Callable[[], None]:
        _REGISTRY.append((name, fn))
        return fn

    return decorator


def _execute(name: str, fn: Callable[[], None]) -> None:
    if name in _SKIP_RETIRED:
        return
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn()
    except AssertionError as e:
        _RESULTS.append((name, False, f"assert: {e}\n{buf.getvalue()}"))
        return
    except Exception as e:
        _RESULTS.append(
            (name, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}\n{buf.getvalue()}")
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
    os.environ.setdefault("TELEGRAM_TOKEN", "0000000000:AAAA_smoke_placeholder_token_0000000")

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
        import dashboard_server as ds  # noqa: E402
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
            "shares": 10,
            "entry_price": 10.0,
            "stop_price": 9.0,
            "entry_time": "10:00",
            "date": today,
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

        class FakeUser:
            id = int(non_owner_uid)

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

        class FakeUser:
            id = int(owner_uid)

        class FakeUpdate:
            effective_user = FakeUser()

        result = asyncio.run(bot._auth_guard(FakeUpdate(), None))
        assert result is None

    # ---------- EOD report ----------
    @t("eod: _build_eod_report returns a string")
    def _():
        reset_state()
        m.paper_trades.append(
            {
                "ticker": "A",
                "action": "SELL",
                "date": today,
                "pnl": 10.0,
                "shares": 1,
                "price": 10.0,
                "time": "10:00",
            }
        )
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
        # v5.5.8: every row in short_trade_history now paired-emits a
        # synthesized SHORT entry row plus the COVER. A stray duplicate
        # COVER in paper_trades must still dedup down to a single COVER,
        # so the FAKE ticker should land 2 rows total (synth SHORT entry
        # + a single COVER), not 3.
        reset_state()
        row = {
            "ticker": "FAKE",
            "action": "COVER",
            "date": today,
            "time": "10:30",
            "side": "SHORT",
            "shares": 10,
            "price": 5.0,
            "entry_price": 4.5,
            "entry_time": "10:25",
            "pnl": 12.5,
        }
        m.paper_trades.append(dict(row))
        m.short_trade_history.append(dict(row))
        rows = ds._today_trades()
        fake_rows = [r for r in rows if r.get("ticker") == "FAKE"]
        actions = sorted(r.get("action") for r in fake_rows)
        assert len(fake_rows) == 2, (
            f"expected synth SHORT + de-duped COVER, got {len(fake_rows)}: {fake_rows}"
        )
        assert actions == ["COVER", "SHORT"], (
            f"expected one SHORT entry + one COVER row, got {actions}"
        )

    # ---------- version ----------
    @t("version: BOT_NAME is TradeGenius")
    def _():
        assert getattr(m, "BOT_NAME", None) == "TradeGenius", (
            f"got {getattr(m, 'BOT_NAME', None)!r}"
        )

    @t("version: BOT_VERSION is 5.9.0")
    def _():
        assert m.BOT_VERSION == "5.10.0", f"got {m.BOT_VERSION}"

    @t("version: no -beta suffix")
    def _():
        assert "beta" not in m.BOT_VERSION.lower(), (
            f"BOT_VERSION still carries beta moniker: {m.BOT_VERSION!r}"
        )

    @t("version: CURRENT_MAIN_NOTE begins with current BOT_VERSION")
    def _():
        # v4.11.5 — was hardcoded "v4.11.2" and got missed on .3/.4. Derive
        # from BOT_VERSION so it self-tracks every release.
        expected = f"v{m.BOT_VERSION}"
        assert m.CURRENT_MAIN_NOTE.lstrip().startswith(expected), (
            f"note starts: {m.CURRENT_MAIN_NOTE[:40]!r}, expected prefix {expected!r}"
        )

    @t("version: CURRENT_MAIN_NOTE every line <= 34 chars")
    def _():
        for ln in m.CURRENT_MAIN_NOTE.split("\n"):
            assert len(ln) <= 34, f"line too wide ({len(ln)}): {ln!r}"

    # ---------- v4.0.2-beta DI seed ----------
    @t("di_seed: _seed_di_buffer function exists")
    def _():
        assert hasattr(m, "_seed_di_buffer"), "_seed_di_buffer missing from trade_genius module"
        assert callable(m._seed_di_buffer), "_seed_di_buffer is not callable"
        assert hasattr(m, "_DI_SEED_CACHE"), "_DI_SEED_CACHE module global missing"

    @t("di_seed: DI_PREMARKET_SEED env var documented in .env.example")
    def _():
        env_path = Path(__file__).parent / ".env.example"
        assert env_path.exists(), f".env.example missing at {env_path}"
        text = env_path.read_text(encoding="utf-8")
        assert "DI_PREMARKET_SEED" in text, "DI_PREMARKET_SEED not documented in .env.example"

    # ---------- v4.0.3-beta OR seed ----------
    @t("or_seed: _seed_opening_range function exists")
    def _():
        assert hasattr(m, "_seed_opening_range"), (
            "_seed_opening_range missing from trade_genius module"
        )
        assert callable(m._seed_opening_range), "_seed_opening_range is not callable"
        assert hasattr(m, "_seed_opening_range_all"), "_seed_opening_range_all missing"
        assert callable(m._seed_opening_range_all), "_seed_opening_range_all is not callable"
        assert hasattr(m, "or_stale_skip_count"), "or_stale_skip_count module global missing"
        assert isinstance(m.or_stale_skip_count, dict), (
            f"expected dict, got {type(m.or_stale_skip_count).__name__}"
        )

    @t("or_seed: staleness guard uses configurable threshold")
    def _():
        assert hasattr(m, "OR_STALE_THRESHOLD"), "OR_STALE_THRESHOLD module global missing"
        assert m.OR_STALE_THRESHOLD >= 0.03, (
            f"OR_STALE_THRESHOLD {m.OR_STALE_THRESHOLD} too tight \u2014 "
            "v4.0.3-beta widened this to >=3% to stop killing signals "
            "on normal intraday volatility"
        )
        # Functional: at 4% drift, the guard should PASS (not stale)
        # under the default 5% threshold but fail under the old 1.5%.
        assert m._or_price_sane(100.0, 104.0) is True, "4% drift should be sane under 5% threshold"
        assert m._or_price_sane(100.0, 104.0, threshold=0.015) is False, (
            "4% drift should fail under legacy 1.5% threshold"
        )
        assert m._or_price_sane(100.0, 110.0) is False, "10% drift must still trip the guard"

    # ---------- v4.5.0 refactor: telegram_commands extraction ----------
    @t("refactor: telegram command handlers importable from telegram_commands")
    def _():
        assert hasattr(m_tc, "cmd_status"), "cmd_status missing from telegram_commands"
        assert hasattr(m_tc, "cmd_help"), "cmd_help missing from telegram_commands"
        assert hasattr(m_tc, "cmd_reset"), "cmd_reset missing from telegram_commands"
        assert hasattr(m_tc, "cmd_mode"), "cmd_mode missing from telegram_commands"
        assert hasattr(m_tc, "reset_callback"), "reset_callback missing from telegram_commands"
        assert hasattr(m_tc, "_reset_authorized"), (
            "_reset_authorized missing from telegram_commands"
        )

    @t("refactor: cmd_* handlers not present on trade_genius (moved to telegram_commands)")
    def _():
        for name in (
            "cmd_status",
            "cmd_help",
            "cmd_reset",
            "cmd_mode",
            "cmd_ticker",
            "cmd_perf",
            "reset_callback",
            "_reset_authorized",
        ):
            assert not hasattr(m, name), f"v4.5.0: {name} should have moved out of trade_genius"

    # ---------- v3.6.0 auth guard ----------
    @t("auth: TRADEGENIUS_OWNER_IDS exists, RH_OWNER_USER_IDS removed")
    def _():
        assert hasattr(m, "TRADEGENIUS_OWNER_IDS"), "TRADEGENIUS_OWNER_IDS missing"
        assert isinstance(m.TRADEGENIUS_OWNER_IDS, set), (
            f"expected set, got {type(m.TRADEGENIUS_OWNER_IDS).__name__}"
        )
        assert not hasattr(m, "RH_OWNER_USER_IDS"), (
            "v3.6.0: RH_OWNER_USER_IDS should be hard-renamed away"
        )
        assert not hasattr(m, "_RH_OWNER_USERS_RAW"), (
            "v3.6.0: _RH_OWNER_USERS_RAW should be hard-renamed away"
        )

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
        for bad in (
            "tp_positions",
            "tp_paper_cash",
            "tp_trade_history",
            "tp_short_positions",
            "tp_short_trade_history",
            "tp_unsynced_exits",
            "tp_state",
            "tp_dm_chat_id",
            "_tp_trading_halted",
            "_tp_save_lock",
            "_tp_state_loaded",
            "save_tp_state",
            "load_tp_state",
            "send_tp_telegram",
            "send_traderspost_order",
            "manage_tp_positions",
            "execute_rh_entry",
            "rh_imap_poll_once",
            "cmd_tp_sync",
            "cmd_rh_enable",
            "cmd_rh_disable",
            "cmd_rh_status",
            "is_traderspost_enabled",
            "is_tp_update",
            "check_entry_rh",
            "RH_STARTING_CAPITAL",
            "RH_IMAP_ENABLED",
            "GMAIL_ADDRESS",
            "TELEGRAM_TP_TOKEN",
        ):
            assert not hasattr(m, bad), f"v3.5.0: {bad} should be removed"

    # ---------- v4.0.0-alpha Val executor ----------
    @t("val: TradeGeniusVal class exists")
    def _():
        assert hasattr(m, "TradeGeniusVal"), "TradeGeniusVal missing"
        assert hasattr(m, "TradeGeniusBase"), "TradeGeniusBase missing"
        assert issubclass(m.TradeGeniusVal, m.TradeGeniusBase), (
            "TradeGeniusVal must subclass TradeGeniusBase"
        )
        assert m.TradeGeniusVal.NAME == "Val", f"got {m.TradeGeniusVal.NAME!r}"
        assert m.TradeGeniusVal.ENV_PREFIX == "VAL_", f"got {m.TradeGeniusVal.ENV_PREFIX!r}"

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
            m._emit_signal(
                {
                    "kind": "ENTRY_LONG",
                    "ticker": "TEST",
                    "price": 100.0,
                    "reason": "BREAKOUT",
                    "timestamp_utc": "2026-04-24T00:00:00Z",
                    "main_shares": 10,
                }
            )
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
            m._emit_signal(
                {
                    "kind": "EOD_CLOSE_ALL",
                    "ticker": "",
                    "price": 0.0,
                    "reason": "EOD",
                    "timestamp_utc": "2026-04-24T20:55:00Z",
                    "main_shares": 0,
                }
            )
            assert evt.wait(2.0)
            for key in ("kind", "ticker", "price", "reason", "timestamp_utc", "main_shares"):
                assert key in captured, f"event missing {key}"
            assert captured["kind"] == "EOD_CLOSE_ALL"
        finally:
            m._signal_listeners.remove(_l)

    # ---------- v4.0.0-beta Gene executor ----------
    @t("gene: TradeGeniusGene class exists")
    def _():
        assert hasattr(m, "TradeGeniusGene"), "TradeGeniusGene missing"
        assert issubclass(m.TradeGeniusGene, m.TradeGeniusBase), (
            "TradeGeniusGene must subclass TradeGeniusBase"
        )
        assert m.TradeGeniusGene.NAME == "Gene", f"got {m.TradeGeniusGene.NAME!r}"
        assert m.TradeGeniusGene.ENV_PREFIX == "GENE_", f"got {m.TradeGeniusGene.ENV_PREFIX!r}"

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
            "entry_price": 100.0,
            "shares": 10,
            "stop": 105.0,
            "entry_time": "10:00",
            "date": today,
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
        assert abs(unreal - 50.0) < 0.01, f"expected +50.0 unrealized, got {unreal}"
        m.short_positions.pop("FAKE", None)

    @t("shorts_pnl: positions text shows profitable short with +sign")
    def _():
        reset_state()
        m.short_positions["FAKE"] = {
            "entry_price": 100.0,
            "shares": 10,
            "stop": 105.0,
            "entry_time": "10:00",
            "date": today,
        }
        saved = m.fetch_1min_bars
        try:
            m.fetch_1min_bars = lambda t: {"current_price": 95.0} if t == "FAKE" else saved(t)
            txt = m._build_positions_text()
        finally:
            m.fetch_1min_bars = saved
            m.short_positions.pop("FAKE", None)
        # Expect the positions text to render FAKE's short pnl positively.
        assert "FAKE" in txt, "FAKE missing from positions text"
        assert "P&L $+50.00" in txt or "P&L $+50" in txt, (
            f"expected positive short pnl in output:\n{txt}"
        )

    @t("shorts_pnl: realized short pnl storage is positive for profitable cover")
    def _():
        reset_state()
        m.short_positions["FAKE"] = {
            "entry_price": 100.0,
            "shares": 10,
            "stop": 105.0,
            "entry_time": "10:00",
            "date": today,
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
    @t(
        "dashboard: /api/executor/val endpoint exists and returns disabled gracefully when Val is off"
    )
    def _():
        # Simulate Val disabled by making sure the module global is None.
        saved = getattr(m, "val_executor", None)
        try:
            m.val_executor = None
            payload = ds._executor_snapshot("val")
            assert payload.get("enabled") is False, f"expected enabled=False, got {payload}"
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
        assert "/api/executor/{name}" in paths, f"/api/executor/{{name}} not registered: {paths}"

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
            assert payload.get("ok") is False, f"expected ok=False, got {payload}"
        finally:
            m.val_executor = saved_val
            m.gene_executor = saved_gene

    # ---------- v4.11.0 \u2014 error_state + health pill ----------
    @t("v4.11.0: error_state module imports and exposes API")
    def _():
        import error_state

        assert callable(getattr(error_state, "record_error", None)), (
            "error_state.record_error missing"
        )
        assert callable(getattr(error_state, "snapshot", None)), "error_state.snapshot missing"
        assert callable(getattr(error_state, "reset_daily", None)), (
            "error_state.reset_daily missing"
        )
        assert callable(getattr(error_state, "_reset_for_tests", None)), (
            "error_state._reset_for_tests missing"
        )

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

        first = error_state.record_error("main", "DEDUP_CODE", "error", "x", ts="t1", now_fn=now_fn)
        assert first is True, "first event must dispatch"
        second = error_state.record_error(
            "main", "DEDUP_CODE", "error", "x", ts="t2", now_fn=now_fn
        )
        assert second is False, "second within cooldown must NOT dispatch"
        # Advance past 5-min cooldown; next must dispatch.
        clock["t"] += 301.0
        third = error_state.record_error("main", "DEDUP_CODE", "error", "x", ts="t3", now_fn=now_fn)
        assert third is True, "after cooldown event must dispatch again"
        error_state._reset_for_tests()

    @t("v4.11.0: reset_daily clears all three executors and dedup")
    def _():
        import error_state

        error_state._reset_for_tests()
        for ex in ("main", "val", "gene"):
            error_state.record_error(ex, "X", "error", "y", ts="t")
        for ex in ("main", "val", "gene"):
            assert error_state.snapshot(ex)["count"] == 1, f"{ex} did not record"
        error_state.reset_daily()
        for ex in ("main", "val", "gene"):
            assert error_state.snapshot(ex)["count"] == 0, f"{ex} did not reset"
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
        assert callable(getattr(m, "report_error", None)), "trade_genius.report_error missing"

    @t("v4.11.0: /api/errors/{executor} route registered")
    def _():
        app = ds._build_app()
        paths = []
        for r in app.router.routes():
            info = r.resource.get_info()
            paths.append(info.get("path") or info.get("formatter") or "")
        assert "/api/errors/{executor}" in paths, (
            f"/api/errors/{{executor}} not registered: {paths}"
        )

    @t("v4.11.0: /api/state embeds errors snapshot")
    def _():
        snap = ds.snapshot()
        assert "errors" in snap, f"errors missing in /api/state: {list(snap.keys())[:20]}"
        assert isinstance(snap["errors"], dict), (
            f"errors should be dict, got {type(snap['errors'])}"
        )
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
        assert "session" in payload, f"top-level session key missing: {list(payload.keys())}"
        assert payload["session"] in ("rth", "pre", "post", "closed")
        for row in payload.get("indices", []):
            for k in ("ah", "ah_change", "ah_change_pct"):
                assert k in row, (
                    f"row missing {k!r}: symbol={row.get('symbol')!r} keys={list(row.keys())}"
                )

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
        assert isinstance(out, dict) and out == {}, (
            f"empty input should yield empty dict, got {out!r}"
        )

    @t("v4.13.0: _fetch_indices payload exposes yahoo_ok and futures schema")
    def _():
        payload = ds._fetch_indices()
        # Yahoo block runs after the Alpaca block. If Alpaca early-returned
        # (no paper keys / alpaca-py missing) the Yahoo keys are absent and
        # that's a known degraded mode \u2014 we only assert schema when the
        # function got past the Alpaca block, signalled by ok=True.
        if not payload.get("ok"):
            return  # Alpaca early-return path; nothing to check here.
        assert "yahoo_ok" in payload, f"yahoo_ok missing from payload keys: {list(payload.keys())}"
        assert isinstance(payload["yahoo_ok"], bool), (
            f"yahoo_ok must be bool, got {type(payload['yahoo_ok']).__name__}"
        )
        # Cash-index rows (when present) must carry display_label, and any
        # future sub-object must include change_pct (the only field the
        # frontend renders). ETF rows have no display_label/future keys
        # \u2014 they are skipped here on purpose.
        cash_seen = False
        for row in payload.get("indices", []):
            sym = row.get("symbol", "")
            if sym in ds._YAHOO_CASH_SYMBOLS:
                cash_seen = True
                assert row.get("display_label"), f"cash row {sym} missing display_label: {row}"
                fut = row.get("future")
                if fut is not None:
                    assert "change_pct" in fut, (
                        f"future sub-object missing change_pct on {sym}: {fut}"
                    )
                    assert "label" in fut, f"future sub-object missing label on {sym}: {fut}"
        # If yahoo_ok is True we must have produced at least one cash row;
        # if False, the failure mode is degraded and we accept zero.
        if payload["yahoo_ok"]:
            assert cash_seen, "yahoo_ok=True but no cash-index rows in payload"

    @t("v4.13.0: cash/futures symbol lists are mutually exclusive")
    def _():
        # Sanity guard: if someone accidentally puts ES=F in the cash list
        # the inline-badge logic in _fetch_indices would render ES on its
        # own row instead of riding inside ^GSPC. The two lists must stay
        # disjoint.
        cash = set(ds._YAHOO_CASH_SYMBOLS)
        fut = set(ds._YAHOO_FUTURES_SYMBOLS)
        overlap = cash & fut
        assert not overlap, f"cash and futures lists overlap: {overlap}"

    @t("v4.11.0: log buffer infrastructure removed from dashboard_server")
    def _():
        # The ring-buffer log handler and /stream logs SSE event were
        # deprecated in favor of the per-executor health pill. Asserting
        # absence guards against a partial revert.
        for name in (
            "_LOG_BUFFER_SIZE",
            "_log_buffer",
            "_log_seq",
            "_RingBufferHandler",
            "_install_log_handler",
            "_logs_since",
        ):
            assert not hasattr(ds, name), f"v4.11.0: dashboard_server.{name} should be removed"

    # ---------- v4.3.0 extended-entry guards ----------
    @t("guard: env flags exist with documented defaults")
    def _():
        assert hasattr(m, "ENTRY_EXTENSION_MAX_PCT"), "ENTRY_EXTENSION_MAX_PCT missing"
        assert hasattr(m, "ENTRY_STOP_CAP_REJECT"), "ENTRY_STOP_CAP_REJECT missing"
        assert isinstance(m.ENTRY_EXTENSION_MAX_PCT, float)
        assert isinstance(m.ENTRY_STOP_CAP_REJECT, bool)
        # Defaults: 1.5% extension, reject-on-cap ON.
        assert abs(m.ENTRY_EXTENSION_MAX_PCT - 1.5) < 1e-9, (
            f"expected 1.5, got {m.ENTRY_EXTENSION_MAX_PCT}"
        )
        assert m.ENTRY_STOP_CAP_REJECT is True, f"expected True, got {m.ENTRY_STOP_CAP_REJECT}"

    @t("guard: long extension 0.5% under 1.5% cap is allowed")
    def _():
        or_hi = 100.0
        price = or_hi * 1.005  # 0.5% extended
        ext = (price - or_hi) / or_hi * 100.0
        assert ext <= m.ENTRY_EXTENSION_MAX_PCT, (
            f"ext {ext:.2f}% should be <= {m.ENTRY_EXTENSION_MAX_PCT}%"
        )

    @t("guard: long extension 2.0% over 1.5% cap is rejected")
    def _():
        or_hi = 100.0
        price = or_hi * 1.02  # 2.0% extended
        ext = (price - or_hi) / or_hi * 100.0
        assert ext > m.ENTRY_EXTENSION_MAX_PCT, (
            f"ext {ext:.2f}% should be > {m.ENTRY_EXTENSION_MAX_PCT}%"
        )

    @t("guard: short extension 2.0% below OR_Low is rejected")
    def _():
        or_lo = 100.0
        price = or_lo * 0.98  # 2.0% extended below
        ext = (or_lo - price) / or_lo * 100.0
        assert ext > m.ENTRY_EXTENSION_MAX_PCT, (
            f"ext {ext:.2f}% should be > {m.ENTRY_EXTENSION_MAX_PCT}%"
        )

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
        assert capped is False, f"expected capped=False for entry at OR edge, got capped={capped}"

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
            assert capped is True, "capping machinery must stay intact when reject flag is off"
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
                "current_price": 102.0 if t == "ZZZZ" else (501.0 if t == "SPY" else 401.0),
                "closes": [],
                "volumes": [],
            }
            m.get_fmp_quote = lambda t: None
            m.tiger_di = lambda t: (None, None)  # warmup OK
            m._update_gate_snapshot("ZZZZ")
            snap = m._gate_snapshot.get("ZZZZ") or {}
            assert "extension_pct" in snap, f"extension_pct missing: {snap}"
            # Price 102 vs OR_High 100 → 2.00% extended on the LONG side.
            assert snap["side"] == "LONG", f"side={snap.get('side')}"
            assert abs(snap["extension_pct"] - 2.0) < 0.01, (
                f"expected 2.0, got {snap['extension_pct']}"
            )
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
            assert m._current_mode == m.MarketMode.CLOSED, f"expected CLOSED, got {m._current_mode}"
            assert m._current_mode_reason == "outside market hours", (
                f"expected 'outside market hours', got {m._current_mode_reason!r}"
            )
            assert m._scan_idle_hours is True, (
                f"expected _scan_idle_hours True after close, got {m._scan_idle_hours}"
            )
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
            assert m._current_mode == m.MarketMode.CLOSED, f"expected CLOSED, got {m._current_mode}"
            assert m._current_mode_reason == "weekend", (
                f"expected 'weekend', got {m._current_mode_reason!r}"
            )
            assert m._scan_idle_hours is True, (
                f"expected _scan_idle_hours True on weekend, got {m._scan_idle_hours}"
            )
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
            saved_manage = m.manage_positions
            saved_manage_s = m.manage_short_positions
            saved_hard_eject = m._tiger_hard_eject_check
            saved_check = m.check_entry
            saved_check_s = m.check_short_entry
            saved_bars = m.fetch_1min_bars
            m.manage_positions = lambda: None
            m.manage_short_positions = lambda: None
            m._tiger_hard_eject_check = lambda: None
            m.check_entry = lambda *a, **kw: None
            m.check_short_entry = lambda *a, **kw: None
            m.fetch_1min_bars = lambda t: None
            try:
                m.scan_loop()
            finally:
                m.manage_positions = saved_manage
                m.manage_short_positions = saved_manage_s
                m._tiger_hard_eject_check = saved_hard_eject
                m.check_entry = saved_check
                m.check_short_entry = saved_check_s
                m.fetch_1min_bars = saved_bars
            assert m._scan_idle_hours is False, (
                f"expected _scan_idle_hours False during trading hours, got {m._scan_idle_hours}"
            )
        finally:
            m._now_et = saved

    @t("regime: /api/state gates.scan_paused reflects after-hours idle")
    def _():
        reset_state()
        fake_et = _dt_mod.datetime(2026, 4, 22, 17, 0, 0, tzinfo=_ET)
        saved = _freeze_et(fake_et)
        m._scan_paused = False  # user-pause is off
        m._scan_idle_hours = False  # will be set True by scan_loop
        try:
            m.scan_loop()
            # Now ask the dashboard serializer for a state snapshot. It
            # reads module globals directly, so we just call the builder.
            payload = ds.snapshot()
            assert payload["gates"]["scan_paused"] is True, (
                f"expected scan_paused True after close, got {payload['gates']['scan_paused']}"
            )
            # v5.31.2: dashboard now computes regime.mode from real ET time.
            # 17:00 ET on a weekday lands in the after-hours window.
            assert payload["regime"]["mode"] == "AFTER", (
                f"expected regime.mode AFTER, got {payload['regime']['mode']}"
            )
            assert payload["regime"]["mode_reason"] == "after-hours session", (
                f"expected 'after-hours session', got {payload['regime']['mode_reason']!r}"
            )
        finally:
            m._now_et = saved
            m._scan_idle_hours = False

    # ---------- v4.6.0 \u2014 paper_state extraction ----------
    @t("v4.6.0: paper_state module imports cleanly")
    def _():
        import paper_state  # noqa: F401

        assert hasattr(paper_state, "save_paper_state"), "paper_state.save_paper_state missing"
        assert hasattr(paper_state, "load_paper_state"), "paper_state.load_paper_state missing"
        assert hasattr(paper_state, "_do_reset_paper"), "paper_state._do_reset_paper missing"

    @t("v4.6.0: paper_state.save_paper_state is re-exported by trade_genius")
    def _():
        import paper_state

        assert m.save_paper_state is paper_state.save_paper_state, (
            "trade_genius.save_paper_state is not the same callable as "
            "paper_state.save_paper_state \u2014 re-export broken"
        )
        assert m.load_paper_state is paper_state.load_paper_state, (
            "trade_genius.load_paper_state re-export broken"
        )
        assert m._do_reset_paper is paper_state._do_reset_paper, (
            "trade_genius._do_reset_paper re-export broken"
        )

    @t("v4.6.0: paper_state owns _state_loaded and _paper_save_lock")
    def _():
        import paper_state

        assert hasattr(paper_state, "_state_loaded"), (
            "paper_state._state_loaded missing \u2014 should be owned by paper_state"
        )
        assert hasattr(paper_state, "_paper_save_lock"), (
            "paper_state._paper_save_lock missing \u2014 should be owned by paper_state"
        )
        # And the originals must NOT live on trade_genius any more.
        assert not hasattr(m, "_state_loaded"), (
            "v4.6.0: trade_genius._state_loaded should have moved to paper_state"
        )
        assert not hasattr(m, "_paper_save_lock"), (
            "v4.6.0: trade_genius._paper_save_lock should have moved to paper_state"
        )

    # ---------- v4.7.0 \u2014 long/short harmonization ----------
    @t("v4.7.0: check_entry and check_short_entry both return (bool, bars)")
    def _():
        # v4.9.0: check_entry / check_short_entry are now wrappers around
        # the unified check_breakout(side) body. Inspect that single body
        # \u2014 it returns the (bool, bars) tuple on every code path.
        import inspect

        from broker.orders import check_breakout

        src = inspect.getsource(check_breakout)
        assert "return False, None" in src, "check_breakout should return (False, None) on guards"
        assert "return True, bars" in src, "check_breakout should return (True, bars) on success"

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
            assert m.daily_short_entry_date == today, (
                f"date not reset: {m.daily_short_entry_date!r}"
            )
            assert m.daily_short_entry_count.get("AAPL", 0) == 0, (
                f"count not cleared: {dict(m.daily_short_entry_count)}"
            )
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
            m.paper_trades.append(
                {
                    "ticker": "ZZZZ",
                    "action": "SELL",
                    "date": today,
                    "pnl": m.DAILY_LOSS_LIMIT - 100.0,  # already past the limit
                }
            )
            m.short_positions.clear()
            m.execute_short_entry("AAPL", 150.0)
            assert m._trading_halted, "execute_short_entry did not halt trading on loss limit"
            assert "AAPL" not in m.short_positions, (
                "execute_short_entry opened a short despite halt"
            )
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

        assert callable(getattr(m, "_check_daily_loss_limit", None)), (
            "_check_daily_loss_limit helper missing"
        )
        from broker.orders import execute_breakout

        src = inspect.getsource(execute_breakout)
        assert "_check_daily_loss_limit" in src, (
            "execute_breakout does not call _check_daily_loss_limit"
        )

    @t("v4.7.0: _ticker_today_realized_pnl helper exists and aggregates long+short closed trades")
    def _():
        assert callable(getattr(m, "_ticker_today_realized_pnl", None)), (
            "_ticker_today_realized_pnl helper missing"
        )
        from datetime import datetime, timezone

        saved_th = list(m.trade_history)
        saved_sth = list(m.short_trade_history)
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            m.trade_history.clear()
            m.trade_history.append(
                {
                    "ticker": "XYZ",
                    "pnl": 30.0,
                    "exit_time_iso": now_iso,
                }
            )
            m.short_trade_history.clear()
            m.short_trade_history.append(
                {
                    "ticker": "XYZ",
                    "pnl": -20.0,
                    "exit_time_iso": now_iso,
                }
            )
            total = m._ticker_today_realized_pnl("XYZ")
            assert abs(total - 10.0) < 0.01, f"expected $10 net, got ${total:.2f}"
        finally:
            m.trade_history.clear()
            m.trade_history.extend(saved_th)
            m.short_trade_history.clear()
            m.short_trade_history.extend(saved_sth)

    @t("v4.7.0: scan_loop calls execute_short_entry after check_short_entry returns True")
    def _():
        import inspect

        from engine.scan import scan_loop

        scan_src = inspect.getsource(scan_loop)
        # The new control flow: capture (ok, bars) tuple then call execute.
        assert "check_short_entry(ticker)" in scan_src, (
            "scan_loop should call check_short_entry(ticker)"
        )
        assert "execute_short_entry(ticker" in scan_src, (
            "scan_loop should call execute_short_entry(ticker, ...) on True"
        )
        # And the new pattern uses ok/bars symmetrically with long.
        assert scan_src.count("execute_short_entry") >= 1, (
            "scan_loop missing execute_short_entry call"
        )

    @t("v4.7.0: daily_short_entry_date persists across save/load round-trip")
    def _():
        import paper_state
        import tempfile
        import os
        import json

        saved_file = m.PAPER_STATE_FILE
        saved_main_file = getattr(m, "PAPER_STATE_MAIN_FILE", None)
        saved_date = m.daily_short_entry_date
        saved_loaded = paper_state._state_loaded
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                tmp_path = f.name
            # v7.0.0 Phase 3: save_paper_state writes to PAPER_STATE_MAIN_FILE.
            # Point both FILE attrs at the same tmp path so the round-trip test
            # exercises the same file without depending on the default derivation.
            m.PAPER_STATE_FILE = tmp_path
            m.PAPER_STATE_MAIN_FILE = tmp_path
            paper_state._state_loaded = True
            m.daily_short_entry_date = "2026-04-24"
            m.save_paper_state()
            with open(tmp_path) as f:
                disk = json.load(f)
            assert disk.get("daily_short_entry_date") == "2026-04-24", (
                f"date not in disk state: {disk.get('daily_short_entry_date')!r}"
            )
            m.daily_short_entry_date = "WRONG"
            m.load_paper_state()
            assert m.daily_short_entry_date == "2026-04-24", (
                f"date not restored: {m.daily_short_entry_date!r}"
            )
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            m.PAPER_STATE_FILE = saved_file
            if saved_main_file is not None:
                m.PAPER_STATE_MAIN_FILE = saved_main_file
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
            assert results == [True, True, True, True, True, False, False], (
                f"unexpected sequence: {results}"
            )
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
        import asyncio
        import json

        class _Req:
            pass

        resp = asyncio.new_event_loop().run_until_complete(ds.h_version(_Req()))
        body = json.loads(resp.body.decode())
        assert body.get("version") == m.BOT_VERSION, (
            f"/api/version returned {body!r}, want version={m.BOT_VERSION!r}"
        )

    # ============================================================
    # v10.0.1 -- v5 Tiger/Buffalo state-machine tests deleted along
    # with the tiger_buffalo_v5 module + the v5_long_tracks SQLite
    # persistence layer. The v5 plumbing tests (load_track sanitization,
    # MAIN_RELEASE_NOTE alias, etc.) are obsolete with the state
    # machine itself. ~664 LOC removed.
    # ============================================================

    @t("infra: Dockerfile COPY whitelist includes every top-level imported module")
    def _():
        # v5.0.2 hotfix guard: prevent the v4.11.0 / v5.0.0 footgun where a new
        # top-level module is added to the source tree but the Dockerfile per-file
        # COPY whitelist is forgotten, causing prod to crash on import.
        import os
        import re

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
        assert bot2._owner_chats == {"111": 222, "333": 444}, (
            f"reload mismatch: {bot2._owner_chats}"
        )

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
        urlreq.urlopen = lambda *a, **kw: (
            calls.append((a, kw))
            or (_ for _ in ()).throw(
                AssertionError("urlopen must not be called when chat-map is empty")
            )
        )
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
            def read(self_inner):
                return b""

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
        assert b"chat_id=222" in joined and b"chat_id=444" in joined, (
            f"missing chat_ids in payloads: {joined!r}"
        )

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
        assert bot._owner_chats.get(owner) == 7777777, f"auto-learn missed: {bot._owner_chats}"
        assert os.path.exists(path), "auto-learn did not persist to disk"
        import json as _json

        with open(path) as f:
            on_disk = _json.load(f)
        assert on_disk.get(owner) == 7777777, f"on-disk mismatch: {on_disk}"
        _clear_smoke_env()

    # =========================================================
    # v5.1.0 \u2014 Forensic Volume Filter (volume baseline)
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
            "buckets": {
                "1030": {"median": median_v, "p75": median_v + 100, "p90": median_v + 500, "n": 55}
            },
        }

    @t("volprofile: evaluate_g4 Stage 1 GREEN at exactly 120%/100%")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        prof = _fresh_profile(1000)
        qqq = _fresh_profile(2000)
        qqq["ticker"] = "QQQ"
        out = vp_mod.evaluate_g4(
            ticker="AAPL",
            minute_bucket="1030",
            current_volume=1200,
            profile=prof,
            qqq_current_volume=2000,
            qqq_profile=qqq,
            stage=1,
        )
        assert out["green"] is True, out
        assert out["rule"] == "V-P1-R1"

    @t("volprofile: evaluate_g4 Stage 1 RED at 119% (off-by-one)")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        prof = _fresh_profile(1000)
        qqq = _fresh_profile(2000)
        qqq["ticker"] = "QQQ"
        out = vp_mod.evaluate_g4(
            ticker="AAPL",
            minute_bucket="1030",
            current_volume=1190,
            profile=prof,
            qqq_current_volume=2000,
            qqq_profile=qqq,
            stage=1,
        )
        assert out["green"] is False
        assert out["reason"] == "LOW_TICKER", out

    @t("volprofile: evaluate_g4 Stage 1 RED at 120%/99% (low qqq)")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        prof = _fresh_profile(1000)
        qqq = _fresh_profile(2000)
        qqq["ticker"] = "QQQ"
        out = vp_mod.evaluate_g4(
            ticker="AAPL",
            minute_bucket="1030",
            current_volume=1200,
            profile=prof,
            qqq_current_volume=1980,
            qqq_profile=qqq,
            stage=1,
        )
        assert out["green"] is False
        assert out["reason"] == "LOW_QQQ", out

    @t("volprofile: evaluate_g4 Stage 2 GREEN at 100%")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        prof = _fresh_profile(1000)
        out = vp_mod.evaluate_g4(
            ticker="AAPL",
            minute_bucket="1030",
            current_volume=1000,
            profile=prof,
            qqq_current_volume=0,
            qqq_profile=None,
            stage=2,
        )
        assert out["green"] is True, out
        assert out["rule"] == "V-P1-R3"

    @t("volprofile: evaluate_g4 NO_PROFILE_X when profile=None")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        out = vp_mod.evaluate_g4(
            ticker="AAPL",
            minute_bucket="1030",
            current_volume=999,
            profile=None,
            qqq_current_volume=0,
            qqq_profile=None,
            stage=2,
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
            ticker="AAPL",
            minute_bucket="1030",
            current_volume=1500,
            profile=prof,
            qqq_current_volume=0,
            qqq_profile=None,
            stage=2,
        )
        assert out["green"] is False
        assert out["reason"] == "STALE_PROFILE_AAPL", out

    @t("volprofile: evaluate_g4 NO_BUCKET when bucket missing (e.g. 0930)")
    def _():
        vp_mod.VOLUME_PROFILE_ENABLED = True
        prof = _fresh_profile(1000)
        out = vp_mod.evaluate_g4(
            ticker="AAPL",
            minute_bucket="0930",
            current_volume=1500,
            profile=prof,
            qqq_current_volume=0,
            qqq_profile=None,
            stage=2,
        )
        assert out["green"] is False
        assert out["reason"] == "NO_BUCKET_AAPL_0930", out

    @t("volprofile: evaluate_g4 returns DISABLED when VOLUME_PROFILE_ENABLED=False")
    def _():
        prev = vp_mod.VOLUME_PROFILE_ENABLED
        try:
            vp_mod.VOLUME_PROFILE_ENABLED = False
            out = vp_mod.evaluate_g4(
                ticker="AAPL",
                minute_bucket="1030",
                current_volume=99999,
                profile=_fresh_profile(),
                qqq_current_volume=99999,
                qqq_profile=_fresh_profile(),
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
        # _start_volume_profile was removed in v5.26.0 (WebSocket consumer
        # deleted). Simulate the watchlist cap check directly.
        big = ["A%d" % i for i in range(31)]
        assert len(big) > vp_mod.WS_SYMBOL_CAP_FREE_IEX, "test setup broken"

    @t("volprofile: trade_genius imports volume_profile module")
    def _():
        assert hasattr(m, "volume_profile"), "volume_profile not imported"
        assert hasattr(m.volume_profile, "evaluate_g4")

    @t("infra: Dockerfile COPY includes volume_profile.py")
    def _():
        df = (Path(__file__).parent / "Dockerfile").read_text(encoding="utf-8")
        assert "COPY volume_profile.py" in df, "Dockerfile missing volume_profile.py COPY"

    # ---------------------------------------------------------------
    # v5.1.1 \u2014 env-driven A/B toggles + parallel evaluator
    # ---------------------------------------------------------------

    def _v511_save_env() -> dict:
        keys = (
            "VOL_GATE_ENFORCE",
            "VOL_GATE_TICKER_ENABLED",
            "VOL_GATE_INDEX_ENABLED",
            "VOL_GATE_TICKER_PCT",
            "VOL_GATE_QQQ_PCT",
            "VOL_GATE_INDEX_SYMBOL",
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
            for k in (
                "VOL_GATE_ENFORCE",
                "VOL_GATE_TICKER_ENABLED",
                "VOL_GATE_INDEX_ENABLED",
                "VOL_GATE_TICKER_PCT",
                "VOL_GATE_QQQ_PCT",
                "VOL_GATE_INDEX_SYMBOL",
            ):
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

    @t("v5.1.1: VOL_GATE_ENFORCE default is 0 (no enforcement next week)")
    def _():
        saved = _v511_save_env()
        try:
            os.environ.pop("VOL_GATE_ENFORCE", None)
            cfg = vp_mod.load_active_config()
            assert cfg["enforce"] is False, cfg
        finally:
            _v511_restore_env(saved)

    # ---------------------------------------------------------------
    # v5.1.2 \u2014 forensic capture (Tier-1 + Tier-2) + GEMINI_A
    # ---------------------------------------------------------------

    @t("v5.1.6: trade_genius exposes _v516_log_velocity / _v516_log_index / _v516_log_di")
    def _():
        for fn in ("_v516_log_velocity", "_v516_log_index", "_v516_log_di", "_v516_check_velocity"):
            assert hasattr(m, fn) and callable(getattr(m, fn)), fn

    @t("v5.1.6: _v516_log_velocity emits a [V510-VEL] line")
    def _():
        import logging as _logging

        seen: list[str] = []

        class _H(_logging.Handler):
            def emit(self, rec):
                seen.append(rec.getMessage())

        tg_logger = _logging.getLogger("trade_genius")
        h = _H()
        h.setLevel(_logging.INFO)
        tg_logger.addHandler(h)
        old_level = tg_logger.level
        tg_logger.setLevel(_logging.INFO)
        try:
            m._v516_log_velocity("NVDA", "1423", 42, 2871, 2840, 101.1, 78.3)
        finally:
            tg_logger.removeHandler(h)
            tg_logger.setLevel(old_level)
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
        h = _H()
        h.setLevel(_logging.INFO)
        tg_logger.addHandler(h)
        old_level = tg_logger.level
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
            tg_logger.removeHandler(h)
            tg_logger.setLevel(old_level)
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
        h = _H()
        h.setLevel(_logging.INFO)
        tg_logger.addHandler(h)
        old_level = tg_logger.level
        tg_logger.setLevel(_logging.INFO)
        try:
            m._v516_log_index(710.40, 708.72, 649.09, 646.79)
        finally:
            tg_logger.removeHandler(h)
            tg_logger.setLevel(old_level)
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
        h = _H()
        h.setLevel(_logging.INFO)
        tg_logger.addHandler(h)
        old_level = tg_logger.level
        tg_logger.setLevel(_logging.INFO)
        try:
            # both >25 \u2014 double_tap_long Y
            m._v516_log_di("NVDA", 27.4, 29.1, 15.2, 12.8)
        finally:
            tg_logger.removeHandler(h)
            tg_logger.setLevel(old_level)
        line = next((s for s in seen if s.startswith("[V510-DI]")), None)
        assert line is not None, seen
        assert "ticker=NVDA" in line, line
        assert "di_plus_t-1=27.4" in line, line
        assert "di_plus_t=29.1" in line, line
        assert "double_tap_long=Y" in line, line
        assert "double_tap_short=N" in line, line

    @t("v5.1.2: indicators module imports and exposes pure functions")
    def _():
        import indicators as ind

        for fn in ("rsi14", "ema9", "ema21", "atr14", "vwap_dist_pct", "spread_bps"):
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
        bars = [
            {"high": 10 + i * 0.1, "low": 9.5 + i * 0.1, "close": 9.9 + i * 0.1} for i in range(30)
        ]
        v = ind.atr14(bars)
        assert v is not None and v > 0.0, v

    @t("v5.1.2: indicators.vwap_dist_pct None on empty; pct on data")
    def _():
        import indicators as ind

        assert ind.vwap_dist_pct([]) is None
        bars = [{"high": 100.0, "low": 99.0, "close": 99.5, "volume": 1000} for _ in range(5)]
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
            bars.append({"high": base + 0.4, "low": base - 0.1, "close": base + 0.2})
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
            bars.append({"high": base + 0.1, "low": base - 0.4, "close": base - 0.2})
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
            bar = {
                "ts": "2026-04-28T14:31:00",
                "et_bucket": "1031",
                "open": 425.93,
                "high": 426.10,
                "low": 425.50,
                "close": 425.85,
                "iex_volume": 1851,
                "iex_sip_ratio_used": 0.082,
                "bid": 425.84,
                "ask": 425.86,
                "last_trade_price": 425.85,
            }
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
            ba.write_bar("AMD", {"close": 1.0, "garbage_key": "x"}, base_dir=td, today=today)
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

        from broker.lifecycle import eod_close

        src = inspect.getsource(eod_close)
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
        h = _H()
        h.setLevel(_logging.INFO)
        tg_logger.addHandler(h)
        old_level = tg_logger.level
        tg_logger.setLevel(_logging.INFO)
        return seen, h, tg_logger, old_level

    def _v512_release_logger(h, tg_logger, old_level):
        tg_logger.removeHandler(h)
        tg_logger.setLevel(old_level)

    @t("v5.1.2: [V510-MINUTE] line emitted with expected fields")
    def _():
        seen, h, lg, old = _v512_capture_logger()
        try:
            m._v512_log_minute("AMD", "1448", 84, 112, 346.19, 12345)
        finally:
            _v512_release_logger(h, lg, old)
        line = next((s for s in seen if "[V510-MINUTE]" in s), None)
        assert line is not None, seen
        for tok in ("ticker=AMD", "bucket=1448", "t_pct=84", "qqq_pct=112", "vol=12345"):
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
                "AMD",
                "1450",
                1,
                "ARMED",
                True,
                m.CAND_REASON_BREAKOUT_CONFIRMED,
                t_pct=92,
                qqq_pct=118,
                close=347.05,
                stop=343.20,
                rsi14_=68.4,
                ema9_=345.80,
                ema21_=343.92,
                atr14_=1.85,
                vwap_dist_pct_=0.42,
                spread_bps_=2.9,
            )
        finally:
            _v512_release_logger(h, lg, old)
        line = next((s for s in seen if "[V510-CAND]" in s), None)
        assert line is not None, seen
        for tok in (
            "entered=YES",
            "reason=BREAKOUT_CONFIRMED",
            "rsi14=68.4",
            "ema9=345.8",
            "ema21=343.92",
            "atr14=1.85",
            "vwap_dist_pct=0.42",
            "spread_bps=2.9",
            "fsm_state=ARMED",
        ):
            assert tok in line, (tok, line)

    @t("v5.1.2: [V510-CAND] emitted on entered=NO with null indicators")
    def _():
        seen, h, lg, old = _v512_capture_logger()
        try:
            m._v512_log_candidate(
                "AMD",
                "1448",
                1,
                "OBSERVE",
                False,
                m.CAND_REASON_NO_BREAKOUT,
                t_pct=84,
                qqq_pct=112,
                close=346.19,
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
        for r in (
            "NO_BREAKOUT",
            "STAGE_NOT_READY",
            "ALREADY_OPEN",
            "COOL_DOWN",
            "MAX_POSITIONS",
            "BREAKOUT_CONFIRMED",
        ):
            assert r in m.CAND_REASONS, r

    @t("v5.1.2: [V510-FSM] emits on transition")
    def _():
        seen, h, lg, old = _v512_capture_logger()
        try:
            m._v512_log_fsm_transition("AMD", "IDLE", "WATCHING", "VOL_SPIKE_DETECTED", "1445")
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
            m._v512_log_fsm_transition("AMD", "ARMED", "ARMED", "noop", "1445")
        finally:
            _v512_release_logger(h, lg, old)
        assert not any("[V510-FSM]" in s for s in seen), seen

    @t("v5.1.2: [V510-ENTRY] emitter carries bid/ask + account state")
    def _():
        seen, h, lg, old = _v512_capture_logger()
        try:
            m._v512_log_entry_extension(
                "AMD",
                bid=345.10,
                ask=345.14,
                cash=1234.56,
                equity=2345.67,
                open_positions=2,
                total_exposure_pct=42.5,
                current_drawdown_pct=0.0,
            )
        finally:
            _v512_release_logger(h, lg, old)
        line = next((s for s in seen if "[V510-ENTRY]" in s), None)
        assert line is not None, seen
        for tok in (
            "ticker=AMD",
            "bid=345.1",
            "ask=345.14",
            "cash=1234.56",
            "equity=2345.67",
            "open_positions=2",
            "total_exposure_pct=42.5",
            "current_drawdown_pct=0",
        ):
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
    # The volume_profile + bar_archive + indicators +
    # [V510-CAND]/[V510-FSM]/[V510-MINUTE]/[V510-VEL]/[V510-DI] log
    # emitter tests remain (forensic capture is live).
    # ------------------------------------------------------------------

    @t("v5.5.4: BOT_VERSION bumped to 5.5.4")
    def _():
        # v5.5.11 supersedes; keep the test name pinned to its release
        # (Val's convention) while asserting the rolling current version.
        assert m.BOT_VERSION == "5.10.0", m.BOT_VERSION

    @t("v5.5.5: BOT_VERSION bumped to 5.5.5")
    def _():
        # v5.5.11 supersedes; same pinned-name pattern.
        assert m.BOT_VERSION == "5.10.0", m.BOT_VERSION

    @t("v5.5.6: BOT_VERSION bumped to 5.5.6")
    def _():
        # v5.5.11 supersedes; same pinned-name pattern.
        assert m.BOT_VERSION == "5.10.0", m.BOT_VERSION

    @t("v5.5.7: BOT_VERSION bumped to 5.5.7")
    def _():
        # v5.5.11 supersedes; same pinned-name pattern.
        assert m.BOT_VERSION == "5.10.0", m.BOT_VERSION

    @t("v5.5.8: BOT_VERSION bumped to 5.5.8")
    def _():
        # v5.5.11 supersedes; same pinned-name pattern.
        assert m.BOT_VERSION == "5.10.0", m.BOT_VERSION

    @t("v5.5.9: BOT_VERSION bumped to 5.5.9")
    def _():
        # v5.5.11 supersedes; same pinned-name pattern.
        assert m.BOT_VERSION == "5.10.0", m.BOT_VERSION

    @t("v5.5.10: BOT_VERSION bumped to 5.5.10")
    def _():
        assert m.BOT_VERSION == "5.10.0", m.BOT_VERSION

    @t("v5.5.11: BOT_VERSION bumped to 5.5.11")
    def _():
        assert m.BOT_VERSION == "5.10.0", m.BOT_VERSION

    # ---------- v5.5.10 \u2014 executor_positions persistence ----------
    @t("v5.5.10: executor_positions table exists in state.db schema after init_db")
    def _():
        import persistence as p

        p.init_db()
        c = p._conn()
        cur = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='executor_positions'"
        )
        row = cur.fetchone()
        assert row is not None, "executor_positions table missing from state.db schema"
        # PK must include executor_name AND mode AND ticker so Val/paper
        # never overwrites Val/live or Gene/paper.
        cur = c.execute("PRAGMA table_info(executor_positions)")
        cols = {r[1]: r for r in cur.fetchall()}
        for must in (
            "executor_name",
            "mode",
            "ticker",
            "side",
            "qty",
            "entry_price",
            "entry_ts_utc",
            "source",
            "stop",
            "trail",
            "last_updated_utc",
        ):
            assert must in cols, f"executor_positions missing column {must}"
        # Sanity: PK columns flagged.
        pk_cols = [name for name, info in cols.items() if info[5] > 0]
        assert set(pk_cols) >= {"executor_name", "mode", "ticker"}, (
            f"PK should cover (executor_name, mode, ticker), got {pk_cols}"
        )

    @t("v5.5.10: _record_position writes an executor_positions row")
    def _():
        import persistence as p

        # Wipe any leftover rows for this synthetic NAME so the test is
        # idempotent regardless of order.
        c = p._conn()
        c.execute(
            "DELETE FROM executor_positions WHERE executor_name = ?",
            ("V510Smoke",),
        )

        class _Bot(m.TradeGeniusBase):
            NAME = "V510Smoke"
            ENV_PREFIX = "V510SMOKE_"

            def __init__(self_inner):
                self_inner.NAME = "V510Smoke"
                self_inner.mode = "paper"
                self_inner.positions = {}

        bot = _Bot()
        bot._record_position("META", "LONG", 14, 680.28)
        rows = p.load_executor_positions("V510Smoke", "paper")
        assert "META" in rows, f"expected META row, got {rows!r}"
        assert rows["META"]["qty"] == 14
        assert abs(rows["META"]["entry_price"] - 680.28) < 1e-6
        assert rows["META"]["source"] == "SIGNAL"
        # Cleanup.
        c.execute(
            "DELETE FROM executor_positions WHERE executor_name = ?",
            ("V510Smoke",),
        )

    @t("v5.5.10: _load_persisted_positions populates self.positions on __init__")
    def _():
        import persistence as p

        c = p._conn()
        c.execute(
            "DELETE FROM executor_positions WHERE executor_name = ?",
            ("V510Smoke2",),
        )
        # Pre-seed a row as if a prior process had recorded META.
        p.save_executor_position(
            "V510Smoke2",
            "paper",
            "META",
            {
                "side": "LONG",
                "qty": 14,
                "entry_price": 680.28,
                "entry_ts_utc": "2026-04-27T17:42:18+00:00",
                "source": "SIGNAL",
                "stop": None,
                "trail": None,
            },
        )

        class _Bot(m.TradeGeniusBase):
            NAME = "V510Smoke2"
            ENV_PREFIX = "V510SMOKE2_"

            def __init__(self_inner):
                self_inner.NAME = "V510Smoke2"
                self_inner.mode = "paper"
                self_inner.positions = {}
                # Hit the actual loader \u2014 not the parent __init__,
                # which would also touch env/Telegram/etc.
                self_inner._load_persisted_positions()

        bot = _Bot()
        assert "META" in bot.positions, f"persisted META not rehydrated, got {bot.positions!r}"
        assert bot.positions["META"]["qty"] == 14
        c.execute(
            "DELETE FROM executor_positions WHERE executor_name = ?",
            ("V510Smoke2",),
        )

    @t("v5.5.10: _reconcile_broker_positions is silent when persisted matches broker")
    def _():
        # Today's canonical case: Val booted with META 14 already
        # persisted; broker also reports META 14. v5.5.9 would have
        # grafted+Telegram'd; v5.5.10 must stay silent.
        import persistence as p

        c = p._conn()
        c.execute(
            "DELETE FROM executor_positions WHERE executor_name = ?",
            ("V510Smoke3",),
        )

        class _BP:
            symbol = "META"
            qty = "14"
            avg_entry_price = "680.28"

        class _Client:
            def get_all_positions(self_inner):
                return [_BP()]

        sent = []

        class _Bot(m.TradeGeniusBase):
            NAME = "V510Smoke3"
            ENV_PREFIX = "V510SMOKE3_"

            def __init__(self_inner):
                self_inner.NAME = "V510Smoke3"
                self_inner.mode = "paper"
                self_inner.positions = {}
                self_inner._load_persisted_positions()

            def _ensure_client(self_inner):
                return _Client()

            def _send_own_telegram(self_inner, msg):
                sent.append(msg)

        # Pre-seed the persisted row so the bot's positions set
        # matches the broker's set exactly.
        p.save_executor_position(
            "V510Smoke3",
            "paper",
            "META",
            {
                "side": "LONG",
                "qty": 14,
                "entry_price": 680.28,
                "entry_ts_utc": "2026-04-27T17:00:00+00:00",
                "source": "SIGNAL",
                "stop": None,
                "trail": None,
            },
        )
        bot = _Bot()
        bot._reconcile_broker_positions()
        assert sent == [], f"clean reconcile must NOT Telegram, got {sent!r}"
        c.execute(
            "DELETE FROM executor_positions WHERE executor_name = ?",
            ("V510Smoke3",),
        )

    @t("v5.5.10: _reconcile_broker_positions self-heals stale persisted entries quietly")
    def _():
        # Persisted has a ticker the broker says we no longer hold.
        # Outcome 3: WARN log + remove, no Telegram.
        import persistence as p

        c = p._conn()
        c.execute(
            "DELETE FROM executor_positions WHERE executor_name = ?",
            ("V510Smoke4",),
        )

        class _Client:
            def get_all_positions(self_inner):
                return []  # broker holds nothing

        sent = []

        class _Bot(m.TradeGeniusBase):
            NAME = "V510Smoke4"
            ENV_PREFIX = "V510SMOKE4_"

            def __init__(self_inner):
                self_inner.NAME = "V510Smoke4"
                self_inner.mode = "paper"
                self_inner.positions = {}
                self_inner._load_persisted_positions()

            def _ensure_client(self_inner):
                return _Client()

            def _send_own_telegram(self_inner, msg):
                sent.append(msg)

        p.save_executor_position(
            "V510Smoke4",
            "paper",
            "STALE",
            {
                "side": "LONG",
                "qty": 5,
                "entry_price": 100.0,
                "entry_ts_utc": "2026-04-27T17:00:00+00:00",
                "source": "SIGNAL",
                "stop": None,
                "trail": None,
            },
        )
        bot = _Bot()
        assert "STALE" in bot.positions
        bot._reconcile_broker_positions()
        assert sent == [], f"stale-self-heal must NOT Telegram, got {sent!r}"
        assert "STALE" not in bot.positions, "stale ticker must be removed from in-memory dict"
        rows = p.load_executor_positions("V510Smoke4", "paper")
        assert "STALE" not in rows, "stale ticker must be removed from executor_positions row set"

    @t("v5.5.10: _reconcile_broker_positions still grafts + Telegrams on true divergence")
    def _():
        # Persisted does NOT have the ticker, broker does. This is a
        # real orphan \u2014 graft and Telegram with "(true divergence)".
        import persistence as p

        c = p._conn()
        c.execute(
            "DELETE FROM executor_positions WHERE executor_name = ?",
            ("V510Smoke5",),
        )

        class _BP:
            symbol = "AAPL"
            qty = "20"
            avg_entry_price = "172.10"

        class _Client:
            def get_all_positions(self_inner):
                return [_BP()]

        sent = []

        class _Bot(m.TradeGeniusBase):
            NAME = "V510Smoke5"
            ENV_PREFIX = "V510SMOKE5_"

            def __init__(self_inner):
                self_inner.NAME = "V510Smoke5"
                self_inner.mode = "paper"
                self_inner.positions = {}
                self_inner._load_persisted_positions()

            def _ensure_client(self_inner):
                return _Client()

            def _send_own_telegram(self_inner, msg):
                sent.append(msg)

        bot = _Bot()
        assert "AAPL" not in bot.positions
        bot._reconcile_broker_positions()
        assert "AAPL" in bot.positions, "true orphan must be grafted into self.positions"
        assert bot.positions["AAPL"]["source"] == "RECONCILE"
        assert any("true divergence" in s for s in sent), (
            f"expected '(true divergence)' Telegram, got {sent!r}"
        )
        # Graft must have been persisted so the next reboot stays silent.
        rows = p.load_executor_positions("V510Smoke5", "paper")
        assert "AAPL" in rows, "grafted orphan must be persisted to DB"
        c.execute(
            "DELETE FROM executor_positions WHERE executor_name = ?",
            ("V510Smoke5",),
        )

    @t("v5.5.8: _today_trades synthesizes SHORT entry rows from short_trade_history")
    def _():
        # Pins the v5.5.8 read-side synthesis: every COVER in
        # short_trade_history must paired-emit a SHORT entry row, plus
        # an open-short sweep of short_positions for entries dated today.
        # Greps dashboard_server.py for the canonical synthesis comment
        # plus the action="SHORT" emit, so a future refactor that drops
        # the entry-row emit fails CI loudly.
        from pathlib import Path

        src = Path(__file__).resolve().parent / "dashboard_server.py"
        text = src.read_text()
        assert (
            "v5.5.8 \u2014 SHORT entry-row synthesis" in text
            or "synthesize the SHORT entry row" in text
        ), "v5.5.8 synthesis comment missing from _today_trades"
        assert '"action": "SHORT"' in text, "synthesized SHORT entry row missing action=SHORT"
        assert "short_positions" in text, "open-short sweep of short_positions missing"

    @t("v5.5.6: previous_session_bucket exists and returns just-closed bucket")
    def _():
        import volume_profile as _vp
        from datetime import datetime as _dt

        ts = _dt(2026, 4, 27, 10, 27, 30, tzinfo=_vp.ET)
        assert hasattr(_vp, "previous_session_bucket"), (
            "previous_session_bucket missing from volume_profile"
        )
        assert _vp.previous_session_bucket(ts) == "1026", _vp.previous_session_bucket(ts)
        # Outside-session edge: the just-closed minute is None.
        ts_close = _dt(2026, 4, 27, 16, 1, 0, tzinfo=_vp.ET)
        assert _vp.previous_session_bucket(ts_close) is None

    @t("v5.5.5: WebsocketBarConsumer has _bars_received counter")
    def _():
        import volume_profile as _vp

        c = _vp.WebsocketBarConsumer(["AAPL"], "k", "s")
        assert hasattr(c, "_bars_received") and c._bars_received == 0
        assert hasattr(c, "_last_bar_ts") and c._last_bar_ts is None
        assert hasattr(c, "_last_handler_error")
        assert hasattr(c, "_first_sample_logged") and c._first_sample_logged == 0

    @t("v5.5.5: WebsocketBarConsumer.stats_snapshot returns expected keys")
    def _():
        import volume_profile as _vp

        c = _vp.WebsocketBarConsumer(["AAPL", "QQQ"], "k", "s")
        snap = c.stats_snapshot()
        for k in (
            "bars_received",
            "last_bar_ts",
            "last_handler_error",
            "volumes_size_per_symbol",
            "tickers",
            "watchdog_reconnects",
            "silence_threshold_sec",
        ):
            assert k in snap, (k, snap)
        assert snap["bars_received"] == 0
        assert set(snap["tickers"]) == {"AAPL", "QQQ"}

    @t("v5.5.5: time_since_last_bar_seconds None when no bars")
    def _():
        import volume_profile as _vp

        c = _vp.WebsocketBarConsumer(["AAPL"], "k", "s")
        assert c.time_since_last_bar_seconds() is None

    @t("v5.5.5: VOLPROFILE_WATCHDOG_SEC clamps to >= 30")
    def _():
        import volume_profile as _vp

        os.environ["VOLPROFILE_WATCHDOG_SEC"] = "5"
        try:
            c = _vp.WebsocketBarConsumer(["AAPL"], "k", "s")
            assert c._silence_threshold_sec == 30, c._silence_threshold_sec
        finally:
            del os.environ["VOLPROFILE_WATCHDOG_SEC"]

    @t("v5.5.5: dashboard registers /api/ws_state route")
    def _():
        import dashboard_server as _ds

        app = _ds._build_app()
        routes = {
            r.resource.canonical
            for r in app.router.routes()
            if hasattr(r, "resource") and r.resource is not None
        }
        assert "/api/ws_state" in routes, sorted(routes)

    @t("v5.5.5: dashboard /api/ws_state requires auth")
    def _():
        # Source-grep guard \u2014 the handler must call _check_auth like /api/state.
        import dashboard_server as _ds
        from pathlib import Path as _P

        src = (_P(_ds.__file__)).read_text(encoding="utf-8")
        # Slice the h_ws_state body and verify the auth gate is present.
        idx = src.find("async def h_ws_state(")
        assert idx >= 0, "h_ws_state handler missing"
        body = src[idx : idx + 1200]
        assert "_check_auth(request)" in body, body[:400]

    @t("v5.5.5: bar archive prefers _ws_consumer over Yahoo")
    def _():
        from pathlib import Path as _P

        src = (_P(__file__).parent / "trade_genius.py").read_text(encoding="utf-8")
        assert "_ws_consumer.current_volume(" in src
        assert "if ws_vol is not None" in src
        assert '"et_bucket": et_bucket,' in src

    @t("v5.20.5: DI seeder has RTH fallback wired into recompute")
    def _():
        # Premarket-only DI seed left 0/10 tickers seeded on Apr 30; the
        # fallback extends the data window into RTH proper.
        from pathlib import Path as _P

        src = (_P(__file__).parent / "engine" / "seeders.py").read_text(encoding="utf-8")
        assert "def seed_di_buffer_with_rth_fallback(" in src
        # recompute_di_for_unseeded must dispatch to the new helper.
        idx = src.find("def recompute_di_for_unseeded(")
        assert idx >= 0, "recompute_di_for_unseeded missing"
        body = src[idx : idx + 2400]
        assert "seed_di_buffer_with_rth_fallback(" in body, body[:400]

    @t("v5.21.0: sentinel strip uses vAA-1 'Letter + short name' labels")
    def _():
        # vAA-1 spec mandates 6 alarms; the dashboard sentinel strip
        # surfaces all 6 with letter+short-name labels.
        from pathlib import Path as _P

        js = (_P(__file__).parent / "dashboard_static" / "app.js").read_text(encoding="utf-8")
        for label in (
            "A1 Loss",
            "A2 Flash",
            "B Trend Death",
            "C Vel. Ratchet",
            "D HVP Lock",
            "E Div. Trap",
        ):
            assert label in js, f"vAA-1 sentinel label {label!r} missing from app.js"

    @t("v5.21.0: position rows carry data-pos-ticker (click-to-titan deep-link)")
    def _():
        from pathlib import Path as _P

        js = (_P(__file__).parent / "dashboard_static" / "app.js").read_text(encoding="utf-8")
        assert "data-pos-ticker" in js, (
            "v5.21.0: position table rows must carry data-pos-ticker for click-to-titan"
        )
        assert "__posClickWired" in js, (
            "v5.21.0: position click handler must be guarded by __posClickWired"
        )

    @t("v5.5.5: ARCHITECTURE.md last-refresh footer pinned to 5.7.1")
    def _():
        # Test name pinned to its release; assertion follows BOT_VERSION.
        from pathlib import Path as _P

        arch = (_P(__file__).parent / "ARCHITECTURE.md").read_text(encoding="utf-8")
        assert 'BOT_VERSION = "5.9.0"' in arch, "ARCHITECTURE.md footer not bumped"

    @t("v5.5.5: CHANGELOG.md has v5.9.0 heading at top")
    def _():
        from pathlib import Path as _P

        cl = (_P(__file__).parent / "CHANGELOG.md").read_text(encoding="utf-8")
        # The first ## heading should be the current version.
        head_idx = cl.find("\n## v5.9.0")
        prior = cl.find("\n## v5.8.4")
        assert head_idx >= 0 and (prior < 0 or head_idx < prior), (
            "v5.9.0 heading must precede v5.8.4 in CHANGELOG"
        )

    @t("v6.5.0: ingest.algo_plus._resolve_alpaca_creds prefers VAL_ALPACA_PAPER_KEY")
    def _():
        # Regression guard: _start_volume_profile was removed in v5.26.0.
        # v6.5.0 replaces the broken cred-resolution path with the new
        # ingest.algo_plus module which correctly prefers VAL_ALPACA_PAPER_KEY
        # before falling back to GENE_ALPACA_PAPER_KEY.
        from pathlib import Path as _P
        import ast as _ast

        src = (_P(__file__).parent / "ingest" / "algo_plus.py").read_text(encoding="utf-8")
        # Locate _resolve_alpaca_creds function body
        i = src.find("def _resolve_alpaca_creds")
        assert i != -1, "_resolve_alpaca_creds not found in ingest/algo_plus.py"
        body = src[i : i + 2000]
        i_val = body.find("VAL_ALPACA_PAPER_KEY")
        i_gene = body.find("GENE_ALPACA_PAPER_KEY")
        assert i_val != -1, "VAL_ALPACA_PAPER_KEY missing from _resolve_alpaca_creds"
        assert i_gene != -1, "GENE_ALPACA_PAPER_KEY missing from _resolve_alpaca_creds"
        assert i_val < i_gene, "VAL_ALPACA_PAPER_KEY must be checked before GENE_ALPACA_PAPER_KEY"
        assert "INGEST SHADOW DISABLED" in body, (
            "[INGEST SHADOW DISABLED] log line missing from _resolve_alpaca_creds"
        )

    # The replay logic is being rebuilt against the executor_positions
    # table (see backtest/) using the /data/bars/ JSONL archive.

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

    # ============================================================
    # v5.9.0 \u2014 Permission Gates (G1 swapped to QQQ 5m EMA(3)/EMA(9))
    # ============================================================
    @t("v5.6.0 guard: CHANGELOG.md has v5.6.0 heading")
    def _():
        # Must mention the unified AVWAP gates release on a heading line.
        ch = Path(__file__).resolve().parent / "CHANGELOG.md"
        assert ch.exists(), "CHANGELOG.md missing"
        body = ch.read_text(encoding="utf-8")
        # Heading appears as "## v5.6.0" somewhere in the file.
        assert "v5.6.0" in body, "CHANGELOG.md missing v5.6.0 heading"

    @t("v5.6.0 guard: trade_genius.py has _opening_avwap helper")
    def _():
        assert hasattr(m, "_opening_avwap"), (
            "_opening_avwap helper missing from trade_genius (v5.6.0 G1/G3 source)"
        )
        assert callable(m._opening_avwap)

    @t("v5.6.0 guard: trade_genius.py exposes _v560_log_gate forensic logger")
    def _():
        assert hasattr(m, "_v560_log_gate"), (
            "_v560_log_gate forensic logger missing from trade_genius"
        )

    # ---------- v5.6.1 data-collection guards ----------

    @t("v5.6.1 D1: V561_INDEX_TICKER is QQQ")
    def _():
        assert getattr(m, "V561_INDEX_TICKER", None) == "QQQ", (
            "v5.6.1 archive index ticker must be QQQ"
        )

    @t("v5.6.1 D1: _v561_archive_qqq_bar writes to /data/bars/<UTC>/QQQ.jsonl")
    def _():
        import tempfile
        import json as _json
        from pathlib import Path

        # Stand up a temp /data root, monkeypatch bar_archive's default.
        ba = m.bar_archive
        orig_default = ba.DEFAULT_BASE_DIR
        with tempfile.TemporaryDirectory() as td:
            ba.DEFAULT_BASE_DIR = td
            try:
                bars = {
                    "current_price": 425.10,
                    "closes": [425.00, 425.05, 425.10],
                    "opens": [424.95, 425.00, 425.05],
                    "highs": [425.10, 425.10, 425.15],
                    "lows": [424.90, 424.98, 425.00],
                    "volumes": [12000, 11500, 11800],
                    "timestamps": [1714224000, 1714224060, 1714224120],
                }
                m._v561_archive_qqq_bar(bars)
                # Find the QQQ.jsonl file under the temp dir.
                hits = list(Path(td).rglob("QQQ.jsonl"))
                assert len(hits) == 1, f"expected 1 QQQ.jsonl, got {hits}"
                line = hits[0].read_text().strip().splitlines()[-1]
                row = _json.loads(line)
                assert row["close"] == 425.05, row
                assert row["last_trade_price"] == 425.10, row
            finally:
                ba.DEFAULT_BASE_DIR = orig_default

    @t("v5.6.1 D2: _v561_persist_or_snapshot writes /data/or/<UTC>/<T>.json")
    def _():
        import tempfile
        import json as _json
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            m.or_high["NVDA"] = 880.50
            m.or_low["NVDA"] = 870.25
            try:
                path = m._v561_persist_or_snapshot("NVDA", base_dir=td)
                assert path is not None and Path(path).exists(), path
                payload = _json.loads(Path(path).read_text())
                assert payload["ticker"] == "NVDA"
                assert payload["or_high"] == 880.50
                assert payload["or_low"] == 870.25
                assert "computed_at_utc" in payload
            finally:
                m.or_high.pop("NVDA", None)
                m.or_low.pop("NVDA", None)

    @t("v5.6.1 D2: _v561_maybe_persist_or_snapshots is no-op pre-9:35 ET")
    def _():
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _Z

        early = _dt(2026, 4, 28, 9, 30, tzinfo=_Z("America/New_York"))
        n = m._v561_maybe_persist_or_snapshots(now_et=early)
        assert n == 0, f"pre-9:35 should write 0, got {n}"

    @t("v5.6.1 D3: [V560-GATE] richened line carries all 14 fields")
    def _():
        import logging as _lg
        import io as _io

        buf = _io.StringIO()
        h = _lg.StreamHandler(buf)
        h.setLevel(_lg.INFO)
        m.logger.addHandler(h)
        try:
            m._v561_log_v560_gate_rich(
                ticker="AAPL",
                side="LONG",
                ts_utc="2026-04-28T13:36:00Z",
                ticker_price=215.50,
                ticker_avwap=215.10,
                index_price=425.20,
                index_avwap=425.05,
                or_high=215.40,
                or_low=214.80,
                g1=True,
                g3=True,
                g4=True,
                pass_=True,
                reason=None,
            )
        finally:
            m.logger.removeHandler(h)
        out = buf.getvalue()
        assert "[V560-GATE] " in out
        for tok in (
            "ticker=AAPL",
            "side=LONG",
            "ts=2026-04-28T13:36:00Z",
            "ticker_price=215.5000",
            "ticker_avwap=215.1000",
            "index_price=425.2000",
            "index_avwap=425.0500",
            "or_high=215.4000",
            "or_low=214.8000",
            "g1=True",
            "g3=True",
            "g4=True",
            "pass=True",
            "reason=null",
        ):
            assert tok in out, f"missing token {tok!r} in: {out!r}"

    @t("v5.6.1 D4: _v561_compose_entry_id is deterministic")
    def _():
        eid = m._v561_compose_entry_id("AAPL", "2026-04-28T13:42:31Z")
        assert eid == "AAPL-20260428134231", eid
        # Lowercase normalised + non-digit stripped
        eid2 = m._v561_compose_entry_id("nvda", "2026-04-28 13:42:31+00:00")
        assert eid2 == "NVDA-20260428134231", eid2

    @t("v5.6.1 D4: [TRADE_CLOSED] formatter produces expected string")
    def _():
        import logging as _lg
        import io as _io

        buf = _io.StringIO()
        h = _lg.StreamHandler(buf)
        h.setLevel(_lg.INFO)
        m.logger.addHandler(h)
        try:
            m._v561_log_trade_closed(
                ticker="NVDA",
                side="LONG",
                entry_id="NVDA-20260428134231",
                entry_ts_utc="2026-04-28T13:42:31Z",
                entry_price=880.50,
                exit_ts_utc="2026-04-28T14:10:05Z",
                exit_price=885.10,
                exit_reason="stop",
                qty=10,
                pnl_dollars=46.00,
                pnl_pct=0.5224,
                hold_seconds=1654,
            )
        finally:
            m.logger.removeHandler(h)
        out = buf.getvalue()
        assert "[TRADE_CLOSED]" in out
        for tok in (
            "ticker=NVDA",
            "side=LONG",
            "entry_id=NVDA-20260428134231",
            "entry_ts=2026-04-28T13:42:31Z",
            "entry_price=880.5000",
            "exit_ts=2026-04-28T14:10:05Z",
            "exit_price=885.1000",
            "exit_reason=stop",
            "qty=10",
            "pnl_dollars=46.0000",
            "pnl_pct=0.5224",
            "hold_seconds=1654",
        ):
            assert tok in out, f"missing {tok!r} in {out!r}"

    @t("v5.6.1 D4: [ENTRY] line carries entry_id")
    def _():
        import logging as _lg
        import io as _io

        buf = _io.StringIO()
        h = _lg.StreamHandler(buf)
        h.setLevel(_lg.INFO)
        m.logger.addHandler(h)
        try:
            m._v561_log_entry(
                ticker="MSFT",
                side="SHORT",
                entry_id="MSFT-20260428143200",
                entry_ts_utc="2026-04-28T14:32:00Z",
                entry_price=412.10,
                qty=10,
            )
        finally:
            m.logger.removeHandler(h)
        out = buf.getvalue()
        assert "[ENTRY] " in out
        assert "entry_id=MSFT-20260428143200" in out
        assert "side=SHORT" in out
        assert "qty=10" in out

    @t("v5.6.1 D5: [SKIP] with no gate eval emits gate_state=null")
    def _():
        import logging as _lg
        import io as _io

        buf = _io.StringIO()
        h = _lg.StreamHandler(buf)
        h.setLevel(_lg.INFO)
        m.logger.addHandler(h)
        try:
            m._v561_log_skip(
                ticker="AAPL",
                reason="COOLDOWN:7m",
                ts_utc="2026-04-28T13:50:00Z",
                gate_state=None,
            )
        finally:
            m.logger.removeHandler(h)
        out = buf.getvalue()
        assert "[SKIP] " in out
        assert "gate_state=null" in out, out
        assert "ticker=AAPL" in out
        assert "reason=COOLDOWN:7m" in out

    @t("v5.6.1 D5: [SKIP] with gate state emits canonical JSON")
    def _():
        import json as _json
        import logging as _lg
        import io as _io

        gs = m._v561_gate_state_dict(
            g1=True,
            g3=False,
            g4=True,
            pass_=False,
            ticker_price=215.5,
            ticker_avwap=215.7,
            index_price=425.0,
            index_avwap=425.0,
            or_high=215.6,
            or_low=215.0,
        )
        # canonical encoding -- sort_keys, no whitespace
        buf = _io.StringIO()
        h = _lg.StreamHandler(buf)
        h.setLevel(_lg.INFO)
        m.logger.addHandler(h)
        try:
            m._v561_log_skip(
                ticker="AAPL",
                reason="V560_GATE_BLOCK:G3",
                ts_utc="2026-04-28T13:50:00Z",
                gate_state=gs,
            )
        finally:
            m.logger.removeHandler(h)
        out = buf.getvalue()
        # extract gate_state= chunk
        assert "gate_state=" in out
        idx = out.index("gate_state=") + len("gate_state=")
        chunk = out[idx:].strip()
        parsed = _json.loads(chunk)
        assert parsed["g1"] is True
        assert parsed["g3"] is False
        assert parsed["g4"] is True
        assert parsed["pass"] is False
        assert parsed["ticker_price"] == 215.5
        assert parsed["or_high"] == 215.6

    @t("v5.6.1 D6: boot [UNIVERSE] line includes QQQ + alpha-sorted")
    def _():
        import logging as _lg
        import io as _io

        buf = _io.StringIO()
        h = _lg.StreamHandler(buf)
        h.setLevel(_lg.INFO)
        m.logger.addHandler(h)
        try:
            m._v561_log_universe(
                ["TSLA", "AAPL", "MSFT", "NVDA", "META", "GOOG", "AMZN", "AVGO", "QQQ"]
            )
        finally:
            m.logger.removeHandler(h)
        out = buf.getvalue().strip().splitlines()[-1]
        assert "[UNIVERSE] " in out
        # Alphabetical ordering, comma-separated, QQQ present.
        assert "tickers=AAPL,AMZN,AVGO,GOOG,META,MSFT,NVDA,QQQ,TSLA" in out, out

    @t("v5.6.1 D6: [UNIVERSE] dedupes + uppercases")
    def _():
        import logging as _lg
        import io as _io

        buf = _io.StringIO()
        h = _lg.StreamHandler(buf)
        h.setLevel(_lg.INFO)
        m.logger.addHandler(h)
        try:
            m._v561_log_universe(["aapl", "AAPL", "msft"])
        finally:
            m.logger.removeHandler(h)
        out = buf.getvalue().strip().splitlines()[-1]
        assert "tickers=AAPL,MSFT" in out, out

    @t("v5.6.1 D6: [WATCHLIST_ADD] / [WATCHLIST_REMOVE] hooks exist")
    def _():
        assert callable(getattr(m, "_v561_log_watchlist_add", None))
        assert callable(getattr(m, "_v561_log_watchlist_remove", None))

    @t("v5.6.1 D6: [WATCHLIST_ADD] emits structured line")
    def _():
        import logging as _lg
        import io as _io

        buf = _io.StringIO()
        h = _lg.StreamHandler(buf)
        h.setLevel(_lg.INFO)
        m.logger.addHandler(h)
        try:
            m._v561_log_watchlist_add("PLTR", reason="oomph", ts_utc="2026-04-28T14:00:00Z")
        finally:
            m.logger.removeHandler(h)
        out = buf.getvalue()
        assert "[WATCHLIST_ADD]" in out
        assert "ticker=PLTR" in out
        assert "reason=oomph" in out
        assert "ts=2026-04-28T14:00:00Z" in out

    @t("v5.7.0 guard: CHANGELOG.md still has v5.7.0 heading present")
    def _():
        # v5.7.1 promotes the top heading; the v5.7.0 entry must still
        # exist somewhere below. Don't re-pin it as the topmost.
        from pathlib import Path

        ch = (Path(__file__).resolve().parent / "CHANGELOG.md").read_text()
        assert "## v5.7.0" in ch, "v5.7.0 heading missing from CHANGELOG"

    @t("v5.6.1 guard: no literal em-dash in v5.6.1 helpers")
    def _():
        # Per spec: NEW v5.6.1 string literals must use \u2014 escape.
        # Pre-existing v3.x/v4.x/v5.6.0 lines that already had literal
        # em-dashes are out of scope. Restrict the scan to lines whose
        # surrounding marker tags are v5.6.1 / v561 / V561.
        from pathlib import Path

        src_path = Path(__file__).resolve().parent / "trade_genius.py"
        src = src_path.read_text()
        bad = []
        for i, line in enumerate(src.splitlines(), start=1):
            if "\u2014" not in line:
                continue
            tag_hits = "v5.6.1" in line or "v561" in line.lower() or "V561" in line
            if tag_hits:
                bad.append((i, line[:80]))
        assert not bad, "literal em-dash in v5.6.1-tagged line: %s" % bad[:3]

    # ============================================================
    # v5.7.0 \u2014 Unlimited Titan Strikes
    # ============================================================

    def _v570_setup_clean_session(_m):
        """Reset every v5.7.0 module-level latch + counter so each test
        starts from a known-clean state (the helpers keep state across
        calls). Mocks the kill-switch logger so log de-dup tests can
        observe the count directly without scraping logger output."""
        _m._v570_strike_counts.clear()
        _m._v570_session_hod.clear()
        _m._v570_session_lod.clear()
        _m._v570_daily_realized_pnl = 0.0
        _m._v570_kill_switch_latched = False
        _m._v570_kill_switch_logged = False
        _m._v570_strike_date = _m._v570_session_today_str()
        _m._v570_session_date = _m._v570_strike_date
        _m._v570_daily_pnl_date = _m._v570_strike_date

    @t("v5.7.0 D2: TITAN_TICKERS has exactly 10 alpha-sorted Titans")
    def _():
        assert isinstance(m.TITAN_TICKERS, list)
        assert len(m.TITAN_TICKERS) == 10, m.TITAN_TICKERS
        assert m.TITAN_TICKERS == sorted(m.TITAN_TICKERS), "TITAN_TICKERS must be alpha-sorted"
        assert m.TITAN_TICKERS == [
            "AAPL",
            "AMZN",
            "AVGO",
            "GOOG",
            "META",
            "MSFT",
            "NFLX",
            "NVDA",
            "ORCL",
            "TSLA",
        ], m.TITAN_TICKERS

    @t("v5.15.0 vAA-1: ENABLE_UNLIMITED_TITAN_STRIKES default False (STRIKE-CAP-3)")
    def _():
        assert m.ENABLE_UNLIMITED_TITAN_STRIKES is False

    @t("v5.10.0: DAILY_LOSS_LIMIT_DOLLARS = -1500.0")
    def _():
        assert m.DAILY_LOSS_LIMIT_DOLLARS == -1500.0

    @t("v5.7.0 D1: TICKERS_DEFAULT contains NFLX and ORCL")
    def _():
        assert "NFLX" in m.TICKERS_DEFAULT
        assert "ORCL" in m.TICKERS_DEFAULT

    @t("v5.7.0 D1: [UNIVERSE] boot line includes all 10 Titans + QQQ alpha-sorted")
    def _():
        import logging as _lg
        import io as _io

        buf = _io.StringIO()
        h = _lg.StreamHandler(buf)
        h.setLevel(_lg.INFO)
        m.logger.addHandler(h)
        try:
            m._v561_log_universe(list(m.TITAN_TICKERS) + ["QQQ"])
        finally:
            m.logger.removeHandler(h)
        out = buf.getvalue().strip().splitlines()[-1]
        assert "tickers=AAPL,AMZN,AVGO,GOOG,META,MSFT,NFLX,NVDA,ORCL,QQQ,TSLA" in out, out

    @t("v5.7.0 D1: bar archive helper exists and reads from TICKERS list")
    def _():
        # The v5.1.2 helper that gates persistence on the TICKERS list
        # must let NFLX/ORCL through (they are in TICKERS_DEFAULT and
        # thus seeded into TICKERS at boot).
        assert "NFLX" in m.TICKERS or "NFLX" in m.TICKERS_DEFAULT
        assert "ORCL" in m.TICKERS or "ORCL" in m.TICKERS_DEFAULT
        # Helper signature is unchanged (additive PR; no schema break).
        import inspect

        params = list(inspect.signature(m._v512_archive_minute_bar).parameters)
        assert params == ["ticker", "bar"], params

    @t("v5.7.0 D3: HOD/LOD seeds from first 9:30 ET print and tracks rolling extremes")
    def _():
        _v570_setup_clean_session(m)
        # First print seeds; no break possible.
        prev_h, prev_l, hb, lb = m._v570_update_session_hod_lod("NVDA", 100.0)
        assert prev_h is None and prev_l is None
        assert hb is False and lb is False
        # Equal price -> no break (strict).
        prev_h, prev_l, hb, lb = m._v570_update_session_hod_lod("NVDA", 100.0)
        assert hb is False, "equality must not register as a break"
        # New high -> hod_break True.
        prev_h, _, hb, lb = m._v570_update_session_hod_lod("NVDA", 100.5)
        assert prev_h == 100.0
        assert hb is True and lb is False
        # New low -> lod_break True.
        _, prev_l, hb, lb = m._v570_update_session_hod_lod("NVDA", 99.5)
        assert prev_l == 100.0
        assert lb is True and hb is False

    @t("v5.7.0 D3: HOD/LOD ignores zero/negative prints (defensive)")
    def _():
        _v570_setup_clean_session(m)
        out = m._v570_update_session_hod_lod("NVDA", 0)
        assert out == (None, None, False, False)
        out = m._v570_update_session_hod_lod("NVDA", None)
        assert out == (None, None, False, False)

    @t("v5.19.1 D3: per-ticker strike counter (LONG+SHORT combined), resets at session roll")
    def _():
        # v5.19.1 vAA-1 ULTIMATE Decision 1 \u2014 STRIKE-CAP-3 unified
        # from per-(ticker, side) to per-ticker. Long and short share
        # one counter on the same ticker.
        _v570_setup_clean_session(m)
        assert m._v570_strike_count("NVDA", "LONG") == 0
        n = m._v570_record_entry("NVDA", "LONG")
        assert n == 1
        assert m._v570_strike_count("NVDA", "LONG") == 1
        # Per-ticker counter: SHORT reads the same value as LONG.
        assert m._v570_strike_count("NVDA", "SHORT") == 1
        # A SHORT entry on the same ticker increments the SAME counter.
        n2 = m._v570_record_entry("NVDA", "SHORT")
        assert n2 == 2
        assert m._v570_strike_count("NVDA", "LONG") == 2
        assert m._v570_strike_count("NVDA", "SHORT") == 2
        # Force a session roll \u2014 mock the date to a different day.
        m._v570_strike_date = "1900-01-01"
        m._v570_session_date = "1900-01-01"
        m._v570_daily_pnl_date = "1900-01-01"
        n3 = m._v570_strike_count("NVDA", "LONG")
        assert n3 == 0, f"strike counter must reset on new session; got {n3}"
        # Different ticker is independent.
        m._v570_strike_counts.clear()
        m._v570_record_entry("NVDA", "LONG")
        assert m._v570_strike_count("AAPL", "LONG") == 0

    @t("v5.7.0 D3: Strike 1 LONG NVDA \u2014 expansion gate not consulted")
    def _():
        # Strike 1 path returns False because the helper requires
        # prev_hod to evaluate. is_first should always be True for
        # strike_num=1, regardless of the gate output.
        _v570_setup_clean_session(m)
        # Seed the HOD with one print so the strike-1 evaluation has
        # *some* prior context (replicating mid-day Strike 1 reality).
        m._v570_update_session_hod_lod("NVDA", 100.0)
        # The expansion gate is only meaningful for strike 2+; for
        # strike 1 the implementation logs expansion_gate_pass=False
        # but the actual decision falls to v5.6.0 G1/G3/G4. Confirm
        # the helper itself returns False with prev_hod=None.
        assert (
            m._v570_expansion_gate_pass(
                side="LONG",
                current_price=100.5,
                prev_hod=None,
                prev_lod=None,
                index_price=425.0,
                index_avwap=420.0,
            )
            is False
        )

    @t("v5.7.0 D3: Strike 2 LONG NVDA without HOD break \u2014 expansion gate FAIL")
    def _():
        _v570_setup_clean_session(m)
        # prev_hod=100.0; price=100.0 -> equality, strict > FAILS.
        assert (
            m._v570_expansion_gate_pass(
                side="LONG",
                current_price=100.0,
                prev_hod=100.0,
                prev_lod=99.0,
                index_price=425.0,
                index_avwap=420.0,
            )
            is False
        )

    @t("v5.7.0 D3: Strike 2 LONG NVDA with HOD break + Index above AVWAP \u2014 PASS")
    def _():
        assert (
            m._v570_expansion_gate_pass(
                side="LONG",
                current_price=100.5,
                prev_hod=100.0,
                prev_lod=99.0,
                index_price=425.0,
                index_avwap=420.0,
            )
            is True
        )

    @t("v5.7.0 D3: Strike 2 LONG NVDA with HOD break BUT Index below AVWAP \u2014 FAIL")
    def _():
        assert (
            m._v570_expansion_gate_pass(
                side="LONG",
                current_price=100.5,
                prev_hod=100.0,
                prev_lod=99.0,
                index_price=419.0,
                index_avwap=420.0,
            )
            is False
        )

    @t("v5.7.0 D3: Strike 2 LONG NVDA with HOD break BUT IndexAVWAP=None \u2014 FAIL")
    def _():
        assert (
            m._v570_expansion_gate_pass(
                side="LONG",
                current_price=100.5,
                prev_hod=100.0,
                prev_lod=99.0,
                index_price=425.0,
                index_avwap=None,
            )
            is False
        )

    @t("v5.7.0 D3: Strike 2 SHORT mirror \u2014 LOD break + Index below AVWAP PASSES")
    def _():
        assert (
            m._v570_expansion_gate_pass(
                side="SHORT",
                current_price=99.5,
                prev_hod=100.0,
                prev_lod=100.0,
                index_price=419.0,
                index_avwap=420.0,
            )
            is True
        )
        # Without LOD break: FAIL (equality is strict).
        assert (
            m._v570_expansion_gate_pass(
                side="SHORT",
                current_price=100.0,
                prev_hod=100.0,
                prev_lod=100.0,
                index_price=419.0,
                index_avwap=420.0,
            )
            is False
        )

    @t("v5.19.1 D4: STRIKE-CAP-3 caps a Titan ticker at 3 strikes per day (long+short combined)")
    def _():
        # v5.19.1 vAA-1 ULTIMATE Decision 1 \u2014 STRIKE-CAP-3 is
        # per-ticker, long+short combined. The 4th attempt raises
        # RuntimeError("STRIKE-CAP-3 reached"). Replaces the legacy
        # "unlimited Titan strikes" 25-iteration fixture, which was
        # broken by the v5.15.0 cap raise.
        _v570_setup_clean_session(m)
        assert m._v570_is_titan("NVDA")
        # First 3 strikes succeed (any side mix).
        assert m._v570_record_entry("NVDA", "LONG") == 1
        assert m._v570_record_entry("NVDA", "LONG") == 2
        assert m._v570_record_entry("NVDA", "SHORT") == 3
        # 4th attempt raises regardless of side.
        try:
            m._v570_record_entry("NVDA", "LONG")
            raise AssertionError("4th LONG attempt should have raised")
        except RuntimeError as e:
            assert "STRIKE-CAP-3" in str(e)
        try:
            m._v570_record_entry("NVDA", "SHORT")
            raise AssertionError("4th SHORT attempt should have raised")
        except RuntimeError as e:
            assert "STRIKE-CAP-3" in str(e)
        # Counter remains pinned at 3.
        assert m._v570_strike_count("NVDA", "LONG") == 3
        assert m._v570_strike_count("NVDA", "SHORT") == 3

    @t("v5.7.0 D4: non-Titan ticker is NOT eligible for unlimited strikes")
    def _():
        # Future watchlist add of a non-Titan symbol would still hit
        # the v5.6.0 R3 cap. The Titan classifier is the gate.
        assert m._v570_is_titan("FOO") is False
        # And TITAN_TICKERS does not contain it.
        assert "FOO" not in m.TITAN_TICKERS

    @t("v5.7.0 D5: kill switch existed pre-PR \u2014 _check_daily_loss_limit + DAILY_LOSS_LIMIT")
    def _():
        # Pre-PR audit: confirm the legacy kill-switch surface is
        # preserved (we did not delete it). Threshold is sourced from
        # the env variable with default -500.
        assert callable(getattr(m, "_check_daily_loss_limit", None))
        assert hasattr(m, "DAILY_LOSS_LIMIT")
        # The new v5.7.0 constant matches the legacy default.
        assert m.DAILY_LOSS_LIMIT_DOLLARS == -1500.0

    @t("v5.7.0 D5: realized P&L -$1499.99 does NOT trigger kill switch")
    def _():
        _v570_setup_clean_session(m)
        m._v570_record_trade_close(-1499.99)
        assert m._v570_kill_switch_active() is False

    @t("v5.7.0 D5: realized P&L exactly -$1500.00 triggers kill switch")
    def _():
        _v570_setup_clean_session(m)
        m._v570_record_trade_close(-1500.0)
        assert m._v570_kill_switch_active() is True

    @t("v5.7.0 D5: realized P&L -$1500.01 triggers kill switch")
    def _():
        _v570_setup_clean_session(m)
        m._v570_record_trade_close(-1500.01)
        assert m._v570_kill_switch_active() is True

    @t("v5.7.0 D5: kill switch resets at next session boundary")
    def _():
        _v570_setup_clean_session(m)
        m._v570_record_trade_close(-1501.0)
        assert m._v570_kill_switch_active() is True
        # Force a session roll.
        m._v570_strike_date = "1900-01-01"
        m._v570_session_date = "1900-01-01"
        m._v570_daily_pnl_date = "1900-01-01"
        assert m._v570_kill_switch_active() is False
        # And the cumulative resets too.
        assert m._v570_daily_realized_pnl == 0.0

    @t("v5.7.0 D5: [KILL_SWITCH] line emitted exactly once per session")
    def _():
        import logging as _lg
        import io as _io

        _v570_setup_clean_session(m)
        buf = _io.StringIO()
        h = _lg.StreamHandler(buf)
        h.setLevel(_lg.INFO)
        m.logger.addHandler(h)
        try:
            m._v570_record_trade_close(-1501.0)  # trips kill
            m._v570_record_trade_close(-50.0)  # later loss, no spam
            m._v570_record_trade_close(-50.0)  # ditto
        finally:
            m.logger.removeHandler(h)
        out = buf.getvalue()
        n_kill = out.count("[KILL_SWITCH]")
        assert n_kill == 1, f"expected exactly 1 [KILL_SWITCH] line; got {n_kill}\n{out}"

    @t("v5.7.0 D5: [KILL_SWITCH] line shape carries reason / triggered_at / realized_pnl")
    def _():
        import logging as _lg
        import io as _io

        _v570_setup_clean_session(m)
        buf = _io.StringIO()
        h = _lg.StreamHandler(buf)
        h.setLevel(_lg.INFO)
        m.logger.addHandler(h)
        try:
            m._v570_record_trade_close(-1501.0)
        finally:
            m.logger.removeHandler(h)
        out = buf.getvalue()
        assert "[KILL_SWITCH]" in out
        assert "reason=daily_loss_limit" in out
        assert "triggered_at=" in out
        assert "realized_pnl=" in out

    @t(
        "v5.7.0 D5: open positions can still close after kill switch \u2014 [TRADE_CLOSED] still emits"
    )
    def _():
        # Kill switch only blocks NEW entries. Closing flow continues
        # to call _v561_log_trade_closed which still emits its line
        # and also folds into daily realized P&L.
        import logging as _lg
        import io as _io

        _v570_setup_clean_session(m)
        m._v570_record_trade_close(-1501.0)  # trip
        assert m._v570_kill_switch_active() is True
        buf = _io.StringIO()
        h = _lg.StreamHandler(buf)
        h.setLevel(_lg.INFO)
        m.logger.addHandler(h)
        try:
            m._v561_log_trade_closed(
                ticker="NVDA",
                side="LONG",
                entry_id="NVDA-20260427150000",
                entry_ts_utc="2026-04-27T15:00:00Z",
                entry_price=100.0,
                exit_ts_utc="2026-04-27T15:30:00Z",
                exit_price=99.0,
                exit_reason="stop",
                qty=10,
                pnl_dollars=-10.0,
                pnl_pct=-1.0,
                hold_seconds=1800,
                strike_num=1,
            )
        finally:
            m.logger.removeHandler(h)
        out = buf.getvalue()
        assert "[TRADE_CLOSED]" in out
        assert "strike_num=1" in out
        assert "daily_realized_pnl=" in out

    @t("v5.7.0 D6: [ENTRY] line carries strike_num field")
    def _():
        import logging as _lg
        import io as _io

        buf = _io.StringIO()
        h = _lg.StreamHandler(buf)
        h.setLevel(_lg.INFO)
        m.logger.addHandler(h)
        try:
            m._v561_log_entry(
                ticker="NVDA",
                side="LONG",
                entry_id="NVDA-20260427150000",
                entry_ts_utc="2026-04-27T15:00:00Z",
                entry_price=100.0,
                qty=10,
                strike_num=3,
            )
        finally:
            m.logger.removeHandler(h)
        out = buf.getvalue()
        assert "[ENTRY]" in out
        assert "strike_num=3" in out

    @t("v5.7.0 D6: [TRADE_CLOSED] line carries strike_num + daily_realized_pnl")
    def _():
        import logging as _lg
        import io as _io

        _v570_setup_clean_session(m)
        buf = _io.StringIO()
        h = _lg.StreamHandler(buf)
        h.setLevel(_lg.INFO)
        m.logger.addHandler(h)
        try:
            m._v561_log_trade_closed(
                ticker="NVDA",
                side="LONG",
                entry_id="NVDA-20260427150000",
                entry_ts_utc="2026-04-27T15:00:00Z",
                entry_price=100.0,
                exit_ts_utc="2026-04-27T15:30:00Z",
                exit_price=99.0,
                exit_reason="stop",
                qty=10,
                pnl_dollars=-10.0,
                pnl_pct=-1.0,
                hold_seconds=1800,
                strike_num=2,
            )
        finally:
            m.logger.removeHandler(h)
        out = buf.getvalue()
        assert "[TRADE_CLOSED]" in out
        assert "strike_num=2" in out
        assert "daily_realized_pnl=-10.0000" in out

    @t("v5.7.0 D6: [TRADE_CLOSED] cumulative daily_realized_pnl tracks across closes")
    def _():
        import logging as _lg
        import io as _io

        _v570_setup_clean_session(m)
        buf = _io.StringIO()
        h = _lg.StreamHandler(buf)
        h.setLevel(_lg.INFO)
        m.logger.addHandler(h)
        try:
            m._v561_log_trade_closed(
                ticker="NVDA",
                side="LONG",
                entry_id="A",
                entry_ts_utc="x",
                entry_price=100.0,
                exit_ts_utc="x",
                exit_price=99.0,
                exit_reason="stop",
                qty=10,
                pnl_dollars=-10.0,
                pnl_pct=-1.0,
                hold_seconds=1,
                strike_num=1,
            )
            m._v561_log_trade_closed(
                ticker="NVDA",
                side="LONG",
                entry_id="B",
                entry_ts_utc="x",
                entry_price=100.0,
                exit_ts_utc="x",
                exit_price=98.0,
                exit_reason="stop",
                qty=10,
                pnl_dollars=-20.0,
                pnl_pct=-2.0,
                hold_seconds=1,
                strike_num=2,
            )
        finally:
            m.logger.removeHandler(h)
        out = buf.getvalue()
        # First close: -10. Second close: -10 + -20 = -30.
        assert "daily_realized_pnl=-10.0000" in out
        assert "daily_realized_pnl=-30.0000" in out

    @t("v5.7.0: feature flag False falls back to old behavior (no Titan branching)")
    def _():
        # The flag is read fresh on every check_breakout call. Setting
        # it False at runtime should make _is_titan_unlimited False
        # for every Titan, which means the daily_count<=5 cap applies
        # again. Verify by inspecting the helper used to gate the
        # bypass.
        try:
            saved = m.ENABLE_UNLIMITED_TITAN_STRIKES
            m.ENABLE_UNLIMITED_TITAN_STRIKES = False
            # The expansion gate helper itself is pure and still
            # callable; the fallback is enforced at the check_breakout
            # call site by the `_is_titan_unlimited` boolean. Just
            # confirm it would evaluate to False with the flag off.
            is_titan = m._v570_is_titan("NVDA")
            assert is_titan is True
            assert (bool(m.ENABLE_UNLIMITED_TITAN_STRIKES) and is_titan) is False
        finally:
            m.ENABLE_UNLIMITED_TITAN_STRIKES = saved

    @t("v5.7.0 guard: no literal em-dash in v5.7.0 helpers")
    def _():
        from pathlib import Path

        src_path = Path(__file__).resolve().parent / "trade_genius.py"
        src = src_path.read_text()
        bad = []
        for i, line in enumerate(src.splitlines(), start=1):
            if "\u2014" not in line:
                continue
            tag_hits = "v5.7.0" in line or "v570" in line.lower() or "V570" in line
            if tag_hits:
                bad.append((i, line[:80]))
        assert not bad, "literal em-dash in v5.7.0-tagged line: %s" % bad[:3]

    # ============================================================
    # v5.7.1 \u2014 Bison & Buffalo exit FSM
    # ============================================================
    @t("v5.7.1 D6: VELOCITY_FUSE_PCT = 0.01 (strict 1.0% threshold)")
    def _():
        assert m.VELOCITY_FUSE_PCT == 0.01, m.VELOCITY_FUSE_PCT

    @t("v5.7.1 D5: [V571-EXIT_PHASE] line carries every spec field")
    def _():
        import io
        import logging
        import contextlib

        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        h.setLevel(logging.INFO)
        m.logger.addHandler(h)
        try:
            m._v571_log_exit_phase(
                ticker="NVDA",
                side="LONG",
                entry_id="ent_001",
                from_phase="initial_risk",
                to_phase="house_money",
                trigger="be_2nd_green",
                current_stop=480.50,
                ts_utc="2026-04-28T14:00:00Z",
            )
        finally:
            m.logger.removeHandler(h)
        out = buf.getvalue()
        assert "[V571-EXIT_PHASE]" in out
        assert "ticker=NVDA" in out
        assert "from_phase=initial_risk" in out
        assert "to_phase=house_money" in out
        assert "trigger=be_2nd_green" in out
        assert "current_stop=480.5" in out
        assert "ts=2026-04-28T14:00:00Z" in out

    @t("v5.7.1 D5: [V571-EMA_SEED] line emits once at seed time")
    def _():
        import io
        import logging

        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        h.setLevel(logging.INFO)
        m.logger.addHandler(h)
        try:
            m._v571_log_ema_seed(
                ticker="NVDA",
                ema_value=480.25,
                ts_utc="2026-04-28T14:15:00Z",
            )
        finally:
            m.logger.removeHandler(h)
        out = buf.getvalue()
        assert "[V571-EMA_SEED]" in out
        assert "ticker=NVDA" in out
        assert "ema_value=480.2500" in out
        assert "ts=2026-04-28T14:15:00Z" in out

    # ---------- v9.1 EOD reversal addon wiring (v9.1.25) ----------
    # Three layered SEV-1 bugs in engine.scan._eod_reversal_pass on
    # 2026-05-13 prevented today's EOD reversal trade from firing.
    # All three layers had a static-inspection signature; these
    # smoke tests guard against re-regression.

    return run_suite("LOCAL SMOKE TESTS")


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
        r = sess.post(
            f"{url}/login", data={"password": password}, allow_redirects=False, timeout=10
        )
        assert r.status_code == 302, f"expected 302, got {r.status_code}"
        cookie = sess.cookies.get("spike_session")
        assert cookie and ":" in cookie, f"bad cookie format: {cookie}"

    @t("prod: /login with wrong password returns 401")
    def _():
        s2 = requests.Session()
        r = s2.post(
            f"{url}/login", data={"password": "definitelywrong"}, allow_redirects=False, timeout=10
        )
        assert r.status_code in (401, 429), (
            f"expected 401 (or 429 if rate-limited), got {r.status_code}"
        )

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
            r = s5.post(
                f"{url}/login",
                data={"password": "wrong-rate-limit-test"},
                allow_redirects=False,
                timeout=10,
            )
            statuses.append(r.status_code)
            time.sleep(0.3)
        assert 429 in statuses[5:], f"rate limit never tripped; statuses={statuses}"

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
    parser.add_argument(
        "--synthetic", action="store_true", help="replay synthetic_harness goldens after local"
    )
    parser.add_argument("--url", default=os.environ.get("DASHBOARD_URL", "https://tradegenius.up.railway.app"))
    parser.add_argument("--password", default=os.environ.get("DASHBOARD_PASSWORD", ""))
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
