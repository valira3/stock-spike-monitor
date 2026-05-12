"""v8.3.17 -- TradeGeniusBase._scaled_signal_qty tests.

Val/Gene Alpaca accounts can be smaller than Main's $100K paper book.
Mirroring main_shares 1:1 on a $35K account hits risk_reject:notional_cap
on big-notional tickers (AMZN at $264 x 284 sh = $75K notional vs Val's
$70K cap). v8.3.17 scales mirror qty by ex_equity / main_equity ratio
so smaller executors stay within their risk budget while still
mirroring direction + ticker.
"""
from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest


# Same telegram stubs as other executor tests.
if "telegram" not in sys.modules:
    _tel = ModuleType("telegram")
    for _name in ("BotCommand", "BotCommandScopeAllPrivateChats", "Update"):
        setattr(_tel, _name, type(_name, (), {}))
    sys.modules["telegram"] = _tel
    _tel_ext = ModuleType("telegram.ext")
    for _name in ("Application", "ApplicationHandlerStop", "CommandHandler",
                  "TypeHandler"):
        setattr(_tel_ext, _name, type(_name, (), {}))
    sys.modules["telegram.ext"] = _tel_ext

from executors.base import TradeGeniusBase


class _FakeExec(TradeGeniusBase):
    NAME = "Val"
    mode = "paper"
    DEFAULT_OWNERS = set()

    def __init__(self):
        self.client = None
        self.positions = {}
        self.telegram_token = ""
        self._owner_chats = {}
        self._last_action_ts = {}
        self._persisted_positions = {}


@pytest.fixture
def patched_equity(monkeypatch):
    """Patch trade_genius.paper_cash + engine.portfolio_equity.resolve_equity
    so tests can dial Main vs ex equity independently."""
    state = {"main_equity": 100_000.0, "ex_equity": 35_000.0}

    # Patch _tg() return value
    import executors.base as exec_base
    fake_tg = SimpleNamespace()
    fake_tg.paper_cash = state["main_equity"]
    monkeypatch.setattr(exec_base, "_tg", lambda: fake_tg)

    # Patch engine.portfolio_equity.resolve_equity
    import engine.portfolio_equity as pe
    monkeypatch.setattr(pe, "resolve_equity", lambda pid: state["ex_equity"])

    def _set(*, main=None, ex=None):
        if main is not None:
            state["main_equity"] = float(main)
            fake_tg.paper_cash = state["main_equity"]
        if ex is not None:
            state["ex_equity"] = float(ex)

    return _set


class TestScaledSignalQty:

    def test_smaller_executor_scales_down(self, patched_equity):
        """Operator's scenario: Main $100K, Val $35K -> ratio 0.35,
        700 sh -> 245 sh."""
        patched_equity(main=100_000.0, ex=35_000.0)
        ex = _FakeExec()
        scaled, ratio = ex._scaled_signal_qty(700)
        assert ratio == pytest.approx(0.35, abs=0.001)
        # int floor of 700 * 0.35 = 244.9999... = 244
        assert scaled == 244

    def test_equal_equity_no_scaling(self, patched_equity):
        patched_equity(main=100_000.0, ex=100_000.0)
        ex = _FakeExec()
        scaled, ratio = ex._scaled_signal_qty(700)
        assert ratio == 1.0
        assert scaled == 700

    def test_larger_executor_no_scaling(self, patched_equity):
        """If ex_equity > main_equity, we don't scale UP -- mirror
        Main's size to keep the strategies aligned. Larger executor
        just has more headroom; not a reason to over-fire."""
        patched_equity(main=100_000.0, ex=200_000.0)
        ex = _FakeExec()
        scaled, ratio = ex._scaled_signal_qty(700)
        assert ratio == 1.0
        assert scaled == 700

    def test_zero_signal_qty_passthrough(self, patched_equity):
        patched_equity(main=100_000.0, ex=35_000.0)
        ex = _FakeExec()
        scaled, ratio = ex._scaled_signal_qty(0)
        assert scaled == 0
        assert ratio == 1.0

    def test_negative_signal_qty_passthrough(self, patched_equity):
        """Defensive: negative qty shouldn't crash the helper."""
        patched_equity(main=100_000.0, ex=35_000.0)
        ex = _FakeExec()
        scaled, ratio = ex._scaled_signal_qty(-5)
        assert scaled == -5
        assert ratio == 1.0

    def test_main_equity_zero_no_scaling(self, patched_equity):
        """Defensive: divide-by-zero guard."""
        patched_equity(main=0.0, ex=35_000.0)
        ex = _FakeExec()
        scaled, ratio = ex._scaled_signal_qty(700)
        assert scaled == 700
        assert ratio == 1.0

    def test_ex_equity_zero_no_scaling(self, patched_equity):
        """Defensive: ex_equity unreadable / 0 -> 1:1 fallback (better
        to let Alpaca reject than to silently drop the trade)."""
        patched_equity(main=100_000.0, ex=0.0)
        ex = _FakeExec()
        scaled, ratio = ex._scaled_signal_qty(700)
        assert scaled == 700
        assert ratio == 1.0

    def test_scale_clamps_to_min_one_share(self, patched_equity):
        """When the ratio is tiny (ex_equity << main_equity AND
        signal_qty * ratio < 1), the helper still returns 1 share
        rather than 0 -- preserving the side/ticker signal even on a
        very small account."""
        patched_equity(main=100_000.0, ex=200.0)  # ratio 0.002
        ex = _FakeExec()
        scaled, ratio = ex._scaled_signal_qty(100)  # 100 * 0.002 = 0.2
        assert scaled == 1
        assert ratio == pytest.approx(0.002, abs=0.0005)

    def test_resolve_equity_raise_no_scaling(self, monkeypatch):
        """Defensive: resolve_equity raising -> 1:1 fallback."""
        import executors.base as exec_base
        fake_tg = SimpleNamespace()
        fake_tg.paper_cash = 100_000.0
        monkeypatch.setattr(exec_base, "_tg", lambda: fake_tg)
        import engine.portfolio_equity as pe
        def _boom(pid):
            raise RuntimeError("alpaca down")
        monkeypatch.setattr(pe, "resolve_equity", _boom)
        ex = _FakeExec()
        scaled, ratio = ex._scaled_signal_qty(700)
        assert scaled == 700
        assert ratio == 1.0

    def test_tg_paper_cash_missing_no_scaling(self, monkeypatch):
        """Defensive: _tg() return without paper_cash -> 1:1 fallback."""
        import executors.base as exec_base
        fake_tg = SimpleNamespace()
        # No paper_cash attr
        monkeypatch.setattr(exec_base, "_tg", lambda: fake_tg)
        import engine.portfolio_equity as pe
        monkeypatch.setattr(pe, "resolve_equity", lambda pid: 35_000.0)
        ex = _FakeExec()
        scaled, ratio = ex._scaled_signal_qty(700)
        # paper_cash falls back to 0, main_equity check guards
        assert scaled == 700
        assert ratio == 1.0


class TestOperatorAmznScenario:
    """v8.3.17 -- operator's exact scenario: AMZN SHORT 284 shares
    at $264.60 = $75,167 notional. Val's notional cap is $69,857
    (equity $34,929 * 2.0). Without scaling, this rejects with
    risk_reject:notional_cap. With v8.3.17 scaling (ratio
    34929/100000 = 0.349), 284 sh -> 99 sh, notional ~$26K which
    fits Val's cap."""

    def test_amzn_short_scales_within_cap(self, patched_equity):
        patched_equity(main=100_000.0, ex=34_929.0)
        ex = _FakeExec()
        scaled, ratio = ex._scaled_signal_qty(284)
        assert ratio == pytest.approx(0.349, abs=0.001)
        # 284 * 0.349 = 99.116 -> int floor = 99
        assert scaled == 99
        # Verify the scaled notional fits Val's $69,857 cap.
        scaled_notional = scaled * 264.60
        val_cap = 34_929.0 * 2.0  # max_concurrent_notional_mult default
        assert scaled_notional < val_cap
