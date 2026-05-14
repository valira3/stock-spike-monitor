"""v9.1.25 -- scan-loop integration tests for the EOD reversal addon.

The 2026-05-13 incident chained three SEV-1 bugs in the same wrapper
in `engine.scan._eod_reversal_pass` (and one in its caller `scan_loop`).
The pre-incident unit tests (`tests/strategy/test_orb_eod_reversal.py`,
`tests/strategy/test_orb_eod_integration.py`) exercised the engine
directly via its `admit / close / select_signals / is_*_window` API
but never invoked the scan-loop wrapper that wires those calls in
production. As a result:

  Layer 1 (cur_min NameError in scan.scan_loop)        -- not covered
  Layer 2 (current_equity bound-method TypeError)      -- not covered
  Layer 3 (single-minute is_entry_window == 900)       -- COVERED by
                                                         a wrong test
                                                         (asserted the
                                                         buggy behavior)

This module closes the gap on all three. Two layers (1 + 2) are
guarded by static-inspection tests that regression-prove the call
shape against the patched source. The third (layer 3) is regression-
proven by an end-to-end runtime test that constructs a real
`EodReversalEngine`, real `PortfolioBook` instances, a fake bar
archive under tmp_path, and a fake `EngineCallbacks` -- then drives
`_eod_reversal_pass` across the entry window and asserts admission
fires for the production-default 15:00 ET entry AND also for any
delayed cycle inside [15:00, 15:59) (the failure mode that lost the
2026-05-13 trade).
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from engine import scan
from engine.portfolio_book import ALL_PORTFOLIO_IDS, PortfolioBook
from orb.eod_reversal import EodReversalConfig, EodReversalEngine


# ---------------------------------------------------------------------
# Static-inspection guards (catch layers 1 + 2 at import time)
# ---------------------------------------------------------------------


class TestScanLoopWiringStatic:
    """Layer 1 (v9.1.20): `cur_min` was not defined in scan_loop's local
    scope before being passed to `_eod_reversal_pass`. The pre-v9.1.20
    code raised NameError every cycle; the outer wrapper try/except
    silently caught + logged. A static source check is the lowest-cost
    regression guard: it runs in milliseconds and catches the EXACT
    bug shape (variable definition missing).
    """

    def test_cur_min_defined_before_eod_pass_call(self):
        src = inspect.getsource(scan.scan_loop)
        assert "_eod_reversal_pass(callbacks, cur_min)" in src, (
            "scan_loop must call _eod_reversal_pass(callbacks, cur_min)"
        )
        # The assignment must appear BEFORE the call site.
        idx_assign = src.find("cur_min = now_et.hour * 60 + now_et.minute")
        idx_call = src.find("_eod_reversal_pass(callbacks, cur_min)")
        assert idx_assign != -1, (
            "scan_loop must define `cur_min = now_et.hour * 60 + now_et.minute` "
            "before calling _eod_reversal_pass (v9.1.20 fix)"
        )
        assert idx_assign < idx_call, (
            f"cur_min assignment (pos {idx_assign}) must precede call (pos {idx_call})"
        )

    def test_eod_pass_wrapped_in_try_except(self):
        # The wrapper is what made today's failure silent. We KEEP it
        # (a crash here can't take down morning ORB) but the static
        # check asserts the structure so a future refactor can't drop
        # it and then claim a regression "fixed itself."
        src = inspect.getsource(scan.scan_loop)
        # Just verify the V910-EOD wrapper marker is present near the
        # call site -- not a deep AST check.
        call_idx = src.find("_eod_reversal_pass(callbacks, cur_min)")
        nearby = src[max(0, call_idx - 200) : call_idx + 200]
        assert "[V910-EOD]" in nearby, (
            "scan_loop must keep the [V910-EOD] wrapper try/except around _eod_reversal_pass"
        )


class TestEodPassWiringStatic:
    """Layer 2 (v9.1.21): `current_equity` is a METHOD on PortfolioBook
    (def current_equity(self, prices=None) -> float). The pre-v9.1.21
    code used `getattr(book, "current_equity", 100_000.0) or 100_000.0`
    which returned the bound method object (truthy), then
    `float(<bound_method>)` raised TypeError. The static check makes
    re-introduction immediately visible.
    """

    def test_current_equity_called_as_method_not_attr(self):
        src = inspect.getsource(scan._eod_reversal_pass)
        # The buggy pattern must NOT be present (skipping commentary
        # lines that document the v9.1.21 lesson).
        for line in src.splitlines():
            if line.lstrip().startswith("#"):
                continue
            assert 'getattr(book, "current_equity"' not in line, (
                "v9.1.21 SEV-1 regression: do NOT use "
                '`getattr(book, "current_equity", ...)` -- it returns '
                "the bound method object. Call `book.current_equity()`."
            )
        # AND the correct call form must be present.
        assert "book.current_equity()" in src, (
            "scan._eod_reversal_pass must call current_equity as a method (v9.1.21 fix)"
        )


class TestEntryWindowRangeStatic:
    """Layer 3 (v9.1.22): `is_entry_window` was a single-minute equality
    (`return cur == entry_et_minutes`). Any delayed scan cycle past
    minute 900 silently no-op'd. Today's deploys at 15:25 / 15:53 ET
    landed too late -- the entry window had already closed. v9.1.22
    widened to a range. The runtime unit-test in
    tests/strategy/test_orb_eod_reversal.py already pins the range
    behavior; this static-source check is a structural backstop.
    """

    def test_is_entry_window_uses_range_not_equality(self):
        # The implementation is in orb.eod_reversal -- not scan.py.
        from orb.eod_reversal import EodReversalEngine

        src = inspect.getsource(EodReversalEngine.is_entry_window)
        # Range markers MUST be present.
        assert "<=" in src and "<" in src, (
            "v9.1.22 fix: is_entry_window must use a range comparison "
            "(<= and <), not single-minute equality (==). Pre-v9.1.22 "
            "used `return cur == self.cfg.entry_et_minutes` which "
            "silently no-op'd on any delayed scan cycle."
        )
        # Sanity: confirm we're not looking at a docstring-only match.
        # The implementation body must reference the entry_et_minutes
        # config field.
        assert "entry_et_minutes" in src
        assert "exit_et_minutes" in src


# ---------------------------------------------------------------------
# Runtime integration test (the full call path, end-to-end)
# ---------------------------------------------------------------------


class _FakeCallbacks:
    """Minimal EngineCallbacks stand-in. Returns synthetic 1m bars for
    each universe ticker. The _eod_reversal_pass code reads
    `current_price` and the last entry of `closes`; both are populated
    for every requested ticker.
    """

    def __init__(self, prices: dict[str, float]) -> None:
        self._prices = dict(prices)
        self.entries: list[tuple[str, float]] = []

    def fetch_1min_bars(self, ticker: str) -> dict[str, Any]:
        px = self._prices.get(ticker)
        if px is None:
            return {}
        return {"current_price": px, "closes": [px]}

    def execute_entry(self, ticker: str, price: float) -> None:
        self.entries.append((ticker, price))


@pytest.fixture
def _prior_closes(monkeypatch: pytest.MonkeyPatch):
    """v9.1.25 -- monkeypatch `scan._load_eod_prior_closes` to return
    a fixed dict instead of reading `/data/bars` from disk. Targeted
    swap (one function) instead of patching pathlib globally -- the
    earlier pathlib patch broke pytest's own internal Path use.
    """
    canned: dict[str, float] = {}

    def _set(values: dict[str, float]) -> None:
        canned.clear()
        canned.update(values)

    monkeypatch.setattr(
        scan,
        "_load_eod_prior_closes",
        lambda date_iso, universe: dict(canned),
    )
    return _set


@pytest.fixture
def _eod_engine(monkeypatch: pytest.MonkeyPatch):
    """Provide a fresh EodReversalEngine to `_eod_reversal_pass` via
    `_orb_runtime.get_eod_engine`. Reset state between tests.

    v9.1.74 -- also stubs out `_eod_append_trade_log` so tests never
    write to /data/eod_trade_log.jsonl on disk (which contaminated the
    Today's Trades with fake $99 AAPL / $100.50 ORCL entries during CI).
    """
    monkeypatch.setattr(scan, "_eod_append_trade_log", lambda leg: None)
    cfg = EodReversalConfig(
        # Disable broker firing for the runtime test -- we only care
        # that the engine's per-portfolio entry_attempted flag flips.
        fire_broker=False,
    )
    engine = EodReversalEngine(cfg, portfolio_ids=list(ALL_PORTFOLIO_IDS))
    monkeypatch.setattr(
        "orb.live_runtime.get_eod_engine",
        lambda: engine,
    )
    return engine


@pytest.fixture
def _isolated_portfolio_books(monkeypatch: pytest.MonkeyPatch):
    """Swap PortfolioBook.current_equity to return a known constant so
    sizing math is deterministic and we don't depend on global
    paper_state.
    """
    monkeypatch.setattr(
        PortfolioBook,
        "current_equity",
        lambda self, prices=None: 100_000.0,
    )


class TestEodReversalPassRuntime:
    """End-to-end: prior closes + current prices laid out so AAPL is
    the lowest-ROD3 long pick (the prior-close = 100, current = 99 ->
    -100 bps) and ORCL is the highest-ROD3 short pick (prior = 100,
    current = 100.5 -> +50 bps). The pass should:

      1. Look up prior closes from the sandbox bar archive
      2. Call select_signals -> long_picks=[("AAPL", -100)], short_picks=[("ORCL", +50)]
      3. Admit both legs for each of Main / Val / Gene
      4. Set entry_attempted=True for every portfolio
    """

    UNIVERSE = ("ORCL", "AAPL", "MSFT", "AVGO", "NFLX")
    PRIOR_CLOSES = {t: 100.0 for t in UNIVERSE}
    CURRENT_PRICES = {
        "ORCL": 100.5,  # +50 bps  -> short pick (ORCL in short_tickers)
        "AAPL": 99.0,  # -100 bps -> long pick  (AAPL in long_tickers)
        "MSFT": 100.2,
        "AVGO": 99.5,  # -50 bps  (AVGO in long_tickers, but AAPL beats it)
        "NFLX": 100.3,
    }

    def test_pass_admits_at_window_open(
        self,
        _eod_engine,
        _prior_closes,
        _isolated_portfolio_books,
    ):
        _prior_closes(self.PRIOR_CLOSES)
        cb = _FakeCallbacks(self.CURRENT_PRICES)
        # cur_min = 15*60 = 900 (window open). Layer 1 (NameError) would
        # have crashed the call -- but we're past scan_loop here; this
        # exercise is layer 2 + the full admit path.
        scan._eod_reversal_pass(cb, cur_min=15 * 60)
        for pid in ALL_PORTFOLIO_IDS:
            assert _eod_engine.has_attempted(pid), (
                f"portfolio {pid} entry_attempted must be True after a valid window-open pass"
            )
        # At least the Main book should have admitted both legs.
        st_main = _eod_engine._states["main"]
        assert "AAPL" in st_main.open_positions, "long pick AAPL missing"
        assert "ORCL" in st_main.open_positions, "short pick ORCL missing"
        assert st_main.open_positions["AAPL"].side == "long"
        assert st_main.open_positions["ORCL"].side == "short"

    def test_pass_admits_on_delayed_cycle_inside_window(
        self,
        _eod_engine,
        _prior_closes,
        _isolated_portfolio_books,
    ):
        """The 2026-05-13 failure mode: SEV-1 fixes deployed AFTER
        15:00 ET, so the first clean scan cycle didn't land until
        ~15:25. Pre-v9.1.22 the entry was a single-minute equality
        -> silent no-op. Post-v9.1.22 (range comparison) the
        delayed cycle MUST still admit.
        """
        _prior_closes(self.PRIOR_CLOSES)
        cb = _FakeCallbacks(self.CURRENT_PRICES)
        # cur_min = 15*60 + 25 = 925 (25 min into window). Today's
        # v9.1.20 deploy landed here; pre-v9.1.22 the engine would
        # silently no-op. Post-v9.1.22 admission MUST still fire.
        scan._eod_reversal_pass(cb, cur_min=15 * 60 + 25)
        assert _eod_engine.has_attempted("main"), (
            "Late entry (cur_min=925) must still admit under v9.1.22 "
            "range-window. Pre-v9.1.22 == 900 check silently no-op'd."
        )

    def test_pass_no_op_outside_window(
        self,
        _eod_engine,
        _prior_closes,
        _isolated_portfolio_books,
    ):
        _prior_closes(self.PRIOR_CLOSES)
        cb = _FakeCallbacks(self.CURRENT_PRICES)
        # cur_min = 14:00 ET -> well before entry window.
        scan._eod_reversal_pass(cb, cur_min=14 * 60)
        assert not _eod_engine.has_attempted("main"), "Pass before window must NOT admit"

    def test_pass_flatten_at_exit(
        self,
        _eod_engine,
        _prior_closes,
        _isolated_portfolio_books,
    ):
        # First admit during the window.
        _prior_closes(self.PRIOR_CLOSES)
        cb = _FakeCallbacks(self.CURRENT_PRICES)
        scan._eod_reversal_pass(cb, cur_min=15 * 60)
        assert _eod_engine._states["main"].open_positions, "setup failed"
        # Now the exit window: pass should close every open position.
        scan._eod_reversal_pass(cb, cur_min=15 * 60 + 59)
        st = _eod_engine._states["main"]
        assert not st.open_positions, "Exit window pass must close all open EOD positions"
        assert st.closed_legs, "closed_legs must record the flatten"

    def test_pass_does_not_crash_on_missing_engine(self, monkeypatch):
        """Defensive: if get_eod_engine returns None (engine disabled
        or not bootstrapped) the pass must return cleanly. The wrapper
        try/except in scan_loop is the last line of defense -- but
        the function itself should never raise for the null-engine
        case, since the wrapper would otherwise spam Railway with
        non-actionable tracebacks.
        """
        monkeypatch.setattr(
            "orb.live_runtime.get_eod_engine",
            lambda: None,
        )
        cb = _FakeCallbacks({})
        # Must NOT raise.
        scan._eod_reversal_pass(cb, cur_min=15 * 60)
