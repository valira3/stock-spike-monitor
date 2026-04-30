"""v5.24.0 \u2014 per-executor position tracking + qty fan-out + EOD dedupe.

Three behavioural fixes covered here:

1. ``broker.orders.close_breakout`` now accepts ``suppress_signal=True``
   so the EOD per-ticker close loop does NOT re-fire EXIT_LONG events
   on top of the canonical EOD_CLOSE_ALL emit. Paper book bookkeeping
   (cash, trade_log, lifecycle log, telegram) is unaffected.
2. Executors honour the paper book's ``main_shares`` field on entry
   signals instead of recomputing via ``_shares_for``. This pins
   Val/Gene/etc. to the same per-ticker quantity the paper book booked
   (fixes the 2x-Entry-1 / 4x-Entry-2 doubling bug).
3. Executors use ``self.positions`` as source of truth on EXIT_LONG /
   EXIT_SHORT (skip when not tracked) and treat Alpaca 40410000
   "position not found" as a benign no-op rather than a Telegram error.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import broker.orders as bo  # noqa: E402
from side import Side  # noqa: E402


# ---------------------------------------------------------------------------
# Stub trade_genius surface (minimal, mirrors test_v5_13_7 pattern)
# ---------------------------------------------------------------------------


class _SideCfg:
    def __init__(self, side):
        self.side = side
        self.positions_attr = "positions" if side.is_long else "short_positions"
        self.trade_history_attr = "trade_history" if side.is_long else "short_trade_history"
        self.history_side_label = "LONG" if side.is_long else "SHORT"
        self.paper_log_close_verb = "SELL" if side.is_long else "COVER"
        self.log_side_label = "LONG" if side.is_long else "SHORT"
        self.exit_signal_kind = "EXIT" if side.is_long else "SHORT_EXIT"
        self.trail_peak_attr = "trail_high" if side.is_long else "trail_low"

    def realized_pnl(self, entry_price, exit_price, shares):
        if self.side.is_long:
            return (exit_price - entry_price) * shares
        return (entry_price - exit_price) * shares

    def close_cash_delta(self, shares, price):
        if self.side.is_long:
            return shares * price
        return -shares * price


class _StubTG:
    BOT_NAME = "TradeGenius"
    BOT_VERSION = "5.25.0"
    TRADE_HISTORY_MAX = 1000
    REASON_LABELS: dict = {}

    def __init__(self, *, ticker, pos):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        self.datetime = datetime
        self._tz = ZoneInfo("America/Chicago")
        self.CONFIGS = {Side.LONG: _SideCfg(Side.LONG), Side.SHORT: _SideCfg(Side.SHORT)}
        self.positions = {ticker: pos}
        self.short_positions = {}
        self.trade_history: list = []
        self.short_trade_history: list = []
        self.paper_trades: list = []
        self.paper_all_trades: list = []
        self._last_exit_time: dict = {}
        self.paper_cash = 100_000.0
        self.signals: list = []
        self.telegrams: list = []
        self.paper_logs: list = []
        self.trade_log_rows: list = []
        self.eot = types.SimpleNamespace(SIDE_LONG="LONG", SIDE_SHORT="SHORT")

        class _EotGlue:
            def clear_position_state(self, *_a, **_kw):
                return None

        self.eot_glue = _EotGlue()

    def fetch_1min_bars(self, _t):
        return None

    def get_fmp_quote(self, _t):
        return None

    def _utc_now_iso(self):
        return "2026-04-30T19:49:59Z"

    def _now_et(self):
        return self.datetime(2026, 4, 30, 15, 49, 59)

    def _now_cdt(self):
        return self.datetime(2026, 4, 30, 14, 49, 59)

    def _to_cdt_hhmm(self, _ts):
        return "14:49 CDT"

    def _engine_clear_phase_bucket(self, *_a, **_kw):
        return None

    def send_telegram(self, msg):
        self.telegrams.append(msg)

    def paper_log(self, msg):
        self.paper_logs.append(msg)

    def _emit_signal(self, event):
        self.signals.append(dict(event))

    def trade_log_append(self, row):
        self.trade_log_rows.append(dict(row))

    def _trade_log_snapshot_pos(self, _pos):
        return {}

    def save_paper_state(self):
        return None

    def _v561_log_trade_closed(self, **_kw):
        return None

    def _v561_compose_entry_id(self, ticker, ts):
        return f"{ticker}-{ts}-1"


@pytest.fixture
def install_stub_tg(monkeypatch):
    def _install(stub):
        monkeypatch.setitem(sys.modules, "trade_genius", stub)
        return stub

    return _install


def _make_long_pos(ticker="AAPL"):
    return {
        "entry_price": 270.0,
        "shares": 18,
        "entry_time": "10:04 CDT",
        "entry_ts_utc": "2026-04-30T15:04:42Z",
        "entry_count": 1,
        "lifecycle_position_id": f"{ticker}_20260430T150442Z_long",
    }


# ---------------------------------------------------------------------------
# Fix #1: EOD suppress_signal
# ---------------------------------------------------------------------------


def test_close_breakout_suppress_signal_skips_emit(install_stub_tg):
    """suppress_signal=True must skip the _emit_signal fan-out while
    keeping all other side effects (Telegram, paper_state, paper_log)
    intact.
    """
    pos = _make_long_pos()
    tg = _StubTG(ticker="AAPL", pos=pos)
    install_stub_tg(tg)

    bo.close_breakout("AAPL", 274.26, Side.LONG, reason="EOD", suppress_signal=True)

    assert tg.signals == [], (
        f"_emit_signal must NOT fire when suppress_signal=True; got {tg.signals!r}"
    )
    # Telegram still fires \u2014 paper book operator still sees the close.
    assert tg.telegrams, "send_telegram must still fire under suppress_signal"
    # Position is still removed from the paper book.
    assert "AAPL" not in tg.positions


def test_close_breakout_default_emits_signal(install_stub_tg):
    """Default behavior (no suppress_signal kwarg) must still emit."""
    pos = _make_long_pos()
    tg = _StubTG(ticker="AAPL", pos=pos)
    install_stub_tg(tg)

    bo.close_breakout("AAPL", 274.26, Side.LONG, reason="STOP")

    assert len(tg.signals) == 1, f"expected exactly one emit; got {tg.signals!r}"
    assert tg.signals[0]["kind"] in ("EXIT", "EXIT_LONG")
    assert tg.signals[0]["main_shares"] == 18


def test_lifecycle_close_position_threads_suppress_signal(install_stub_tg):
    """The thin lifecycle.close_position wrapper must forward
    suppress_signal to close_breakout."""
    from broker.lifecycle import close_position

    pos = _make_long_pos()
    tg = _StubTG(ticker="AAPL", pos=pos)
    install_stub_tg(tg)

    close_position("AAPL", 274.26, reason="EOD", suppress_signal=True)
    assert tg.signals == [], "lifecycle.close_position did not forward suppress_signal"


# ---------------------------------------------------------------------------
# Fix #2: executors honour main_shares from the signal
# ---------------------------------------------------------------------------


def _make_executor(monkeypatch, paper_dollars_per_entry=10000.0):
    """Construct a minimal stub executor with a fake Alpaca client so we
    can drive _on_signal directly."""
    monkeypatch.setenv("SSM_SMOKE_TEST", "1")
    monkeypatch.setenv("TEST_ALPACA_PAPER_KEY", "dummy_paper_key")
    monkeypatch.setenv("TEST_ALPACA_PAPER_SECRET", "dummy_paper_secret")
    # Import trade_genius first so constants like TRADEGENIUS_OWNER_IDS
    # are populated and the __main__/trade_genius module alias is set
    # before TradeGeniusBase.__init__ tries to read them.
    if "trade_genius" in sys.modules:
        del sys.modules["trade_genius"]
    import trade_genius  # noqa: F401
    from executors import TradeGeniusBase

    class _StubExec(TradeGeniusBase):
        NAME = "TestStub"
        ENV_PREFIX = "TEST_"

    inst = _StubExec()
    inst.dollars_per_entry = paper_dollars_per_entry

    submits: list = []
    closes: list = []
    raise_404_on_close = {"flag": False}

    class _FakeAcct:
        equity = 200_000.0
        cash = 200_000.0
        buying_power = 400_000.0

    class _FakeClient:
        def get_account(self):
            return _FakeAcct()

        def submit_order(self, req):
            submits.append(req)
            return types.SimpleNamespace(id="fake-order-id")

        def close_position(self, ticker):
            if raise_404_on_close["flag"]:
                raise Exception('{"code":40410000,"message":"position not found: ' + ticker + '"}')
            closes.append(ticker)

        def close_all_positions(self, cancel_orders=False):
            closes.append(("ALL", cancel_orders))

    fake_client = _FakeClient()
    inst._ensure_client = lambda: fake_client  # type: ignore
    inst._send_own_telegram = lambda _msg: None  # type: ignore
    # Pin _shares_for so the legacy fallback is deterministic.
    inst._submit_order_idempotent = lambda client, req, coid: client.submit_order(req)  # type: ignore
    inst._build_client_order_id = lambda ticker, side: f"{ticker}-{side}-coid"  # type: ignore
    # Make _persist_position / _delete_persisted_position no-ops so we don't need a real DB.
    inst._persist_position = lambda _t: None  # type: ignore
    inst._delete_persisted_position = lambda _t: None  # type: ignore

    return inst, submits, closes, raise_404_on_close


def test_executor_uses_signal_main_shares_for_entry(monkeypatch):
    """ENTRY_LONG with main_shares=18 must size 18, not the executor's
    own $10k/$price formula (which would be ~36 at $274)."""
    inst, submits, _closes, _raise = _make_executor(monkeypatch)
    inst._on_signal(
        {
            "kind": "ENTRY_LONG",
            "ticker": "AAPL",
            "price": 274.26,
            "reason": "ENTRY_1",
            "timestamp_utc": "2026-04-30T15:04:42Z",
            "main_shares": 18,
        }
    )
    assert len(submits) == 1, f"expected one submit; got {submits!r}"
    assert submits[0].qty == 18, (
        f"executor must mirror paper book qty=18, not recompute; got qty={submits[0].qty}"
    )
    assert "AAPL" in inst.positions
    assert inst.positions["AAPL"]["qty"] == 18


def test_executor_falls_back_to_shares_for_when_main_shares_missing(monkeypatch):
    """If the signal omits main_shares (back-compat), fall back to
    legacy _shares_for sizing."""
    inst, submits, _closes, _raise = _make_executor(monkeypatch, paper_dollars_per_entry=5000.0)
    # No main_shares key.
    inst._on_signal(
        {
            "kind": "ENTRY_LONG",
            "ticker": "MSFT",
            "price": 100.0,
            "reason": "LEGACY",
            "timestamp_utc": "2026-04-30T15:04:42Z",
        }
    )
    assert len(submits) == 1
    # $5000 / $100 = 50 shares from _shares_for legacy path (or capped).
    assert submits[0].qty > 0
    # Position recorded.
    assert "MSFT" in inst.positions


def test_executor_zero_main_shares_falls_back(monkeypatch):
    """main_shares=0 is treated as missing and falls back to _shares_for."""
    inst, submits, _closes, _raise = _make_executor(monkeypatch, paper_dollars_per_entry=10000.0)
    inst._on_signal(
        {
            "kind": "ENTRY_LONG",
            "ticker": "GOOG",
            "price": 100.0,
            "reason": "X",
            "timestamp_utc": "2026-04-30T15:04:42Z",
            "main_shares": 0,
        }
    )
    assert len(submits) == 1
    assert submits[0].qty > 0


# ---------------------------------------------------------------------------
# Fix #3: idempotent close + self.positions as source of truth
# ---------------------------------------------------------------------------


def test_executor_exit_skipped_when_not_tracked(monkeypatch):
    """EXIT_LONG for a ticker the executor never opened must NOT call
    Alpaca close_position (no spurious 40410000)."""
    inst, _submits, closes, _raise = _make_executor(monkeypatch)
    # self.positions is empty.
    inst._on_signal(
        {
            "kind": "EXIT_LONG",
            "ticker": "AAPL",
            "price": 274.26,
            "reason": "EOD",
            "timestamp_utc": "2026-04-30T19:49:59Z",
        }
    )
    assert closes == [], (
        f"close_position must NOT be called when ticker not in self.positions; got {closes!r}"
    )


def test_executor_exit_idempotent_on_40410000(monkeypatch):
    """When Alpaca returns 40410000 (already flat), the executor must
    swallow the error, drop the local row, and not raise."""
    inst, _submits, _closes, raise_404 = _make_executor(monkeypatch)
    # Seed a position so the EXIT path actually calls close_position.
    inst._record_position("AAPL", "LONG", 18, 270.0)
    raise_404["flag"] = True

    telegrams: list = []
    inst._send_own_telegram = lambda msg: telegrams.append(msg)  # type: ignore

    inst._on_signal(
        {
            "kind": "EXIT_LONG",
            "ticker": "AAPL",
            "price": 274.26,
            "reason": "EOD",
            "timestamp_utc": "2026-04-30T19:49:59Z",
        }
    )
    # Position dropped locally despite the 404.
    assert "AAPL" not in inst.positions
    # No \u274c error telegram.
    assert not any("\u274c" in m for m in telegrams), (
        f"40410000 must not surface as \u274c on Telegram; got {telegrams!r}"
    )


def test_executor_exit_propagates_real_alpaca_errors(monkeypatch):
    """Errors that are NOT 40410000 must still surface as a Telegram error."""
    inst, _submits, _closes, _raise = _make_executor(monkeypatch)
    inst._record_position("AAPL", "LONG", 18, 270.0)

    telegrams: list = []
    inst._send_own_telegram = lambda msg: telegrams.append(msg)  # type: ignore

    def _boom(_t):
        raise Exception("500 Internal Server Error")

    inst._ensure_client().close_position = _boom  # type: ignore

    inst._on_signal(
        {
            "kind": "EXIT_LONG",
            "ticker": "AAPL",
            "price": 274.26,
            "reason": "STOP",
            "timestamp_utc": "2026-04-30T19:49:59Z",
        }
    )
    assert any("\u274c" in m and "500" in m for m in telegrams), (
        f"non-40410000 errors must page operator; got {telegrams!r}"
    )
