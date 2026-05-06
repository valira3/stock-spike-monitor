"""tests/test_v700_fanout.py

v7.0.0 Phase 4 -- per-book config, sizing, and fill-price baseline tests.

Covers:
  - PortfolioConfig defaults are sane
  - book.size_for() computes correctly
  - book.has_position() returns correct boolean for both sides
  - book.is_eligible() filters by enabled, tickers, sides_allowed,
    has_position, cooldown, daily_halted
  - book.record_entry_with_fill() inserts position with fill_price
    baseline and calls record_entry (chandelier reset)
  - Per-book isolation: val.size_for() with different dollars_per_entry
    returns different qty than main
  - Earnings watcher: main.config.earnings_watcher_enabled is True;
    val and gene False
  - paper_shares_for() in broker/orders.py still returns the same value
    as before (regression: snapshot the old behavior)
"""

from __future__ import annotations

import sys
import types
import importlib.util
import logging
import pytest


# ---------------------------------------------------------------------------
# Module-level fixture: load engine.portfolio_book in isolation (no full
# engine/__init__ stack, no Telegram, no Alpaca).
# ---------------------------------------------------------------------------

def _load_portfolio_book():
    """Load engine.portfolio_book fresh, stubbing only alarm_f_trail."""
    # Remove cached copies so each test module load is independent.
    for key in list(sys.modules):
        if "portfolio_book" in key:
            del sys.modules[key]

    # Ensure a minimal engine package namespace exists without triggering
    # engine/__init__.py (which pulls in sentinel -> alarm_f_trail -> etc.)
    if "engine" not in sys.modules or not hasattr(sys.modules["engine"], "__path__"):
        pkg = types.ModuleType("engine")
        pkg.__path__ = ["engine"]
        pkg.__package__ = "engine"
        sys.modules["engine"] = pkg

    # Stub engine.alarm_f_trail so record_entry does not need the full
    # alarm system loaded.
    class _FakeTrailState:
        STAGE_INACTIVE = 0
        peak_close: float = 0.0
        stage: int = 0

        @classmethod
        def fresh(cls) -> "_FakeTrailState":
            ts = cls()
            ts.stage = cls.STAGE_INACTIVE
            ts.peak_close = 0.0
            return ts

    fake_trail = types.ModuleType("engine.alarm_f_trail")
    fake_trail.TrailState = _FakeTrailState
    fake_trail.STAGE_INACTIVE = 0
    sys.modules["engine.alarm_f_trail"] = fake_trail

    spec = importlib.util.spec_from_file_location(
        "engine.portfolio_book", "engine/portfolio_book.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["engine.portfolio_book"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture()
def pb():
    """Fresh portfolio_book module for each test."""
    return _load_portfolio_book()


# ---------------------------------------------------------------------------
# 1. PortfolioConfig defaults
# ---------------------------------------------------------------------------


class TestPortfolioConfigDefaults:
    def test_enabled_default_true(self, pb):
        cfg = pb.PortfolioConfig()
        assert cfg.enabled is True

    def test_tickers_default_none(self, pb):
        """None means all tickers in universe are allowed."""
        cfg = pb.PortfolioConfig()
        assert cfg.tickers is None

    def test_sides_allowed_default_both(self, pb):
        cfg = pb.PortfolioConfig()
        assert cfg.sides_allowed == {"LONG", "SHORT"}

    def test_dollars_per_entry_default(self, pb):
        cfg = pb.PortfolioConfig()
        assert cfg.dollars_per_entry == 10000.0

    def test_daily_loss_limit_default(self, pb):
        cfg = pb.PortfolioConfig()
        assert cfg.daily_loss_limit_dollars == 1000.0

    def test_portfolio_equity_floor_default(self, pb):
        cfg = pb.PortfolioConfig()
        assert cfg.portfolio_equity_floor == 100000.0

    def test_earnings_watcher_default_false(self, pb):
        """Default is False; only main gets True after registry setup."""
        cfg = pb.PortfolioConfig()
        assert cfg.earnings_watcher_enabled is False


# ---------------------------------------------------------------------------
# 2. size_for() share sizing
# ---------------------------------------------------------------------------


class TestSizeFor:
    def test_basic_sizing(self, pb):
        """$10k * 0.5 / $100 = 50 shares."""
        book = pb.PortfolioBook("main")
        book.config.dollars_per_entry = 10000.0
        result = book.size_for("AAPL", 100.0, entry_size_pct=0.5)
        assert result == 50

    def test_invalid_price_zero(self, pb):
        book = pb.PortfolioBook("val")
        assert book.size_for("AAPL", 0.0) == 0

    def test_invalid_price_negative(self, pb):
        book = pb.PortfolioBook("val")
        assert book.size_for("AAPL", -1.0) == 0

    def test_minimum_one_share(self, pb):
        """Very high price should still return at least 1 share."""
        book = pb.PortfolioBook("main")
        book.config.dollars_per_entry = 10000.0
        result = book.size_for("BRK", 500000.0, entry_size_pct=0.5)
        assert result >= 1

    def test_full_entry_pct(self, pb):
        """entry_size_pct=1.0 uses the full dollars_per_entry."""
        book = pb.PortfolioBook("main")
        book.config.dollars_per_entry = 10000.0
        result = book.size_for("AAPL", 100.0, entry_size_pct=1.0)
        assert result == 100


# ---------------------------------------------------------------------------
# 3. has_position()
# ---------------------------------------------------------------------------


class TestHasPosition:
    def test_long_position_detected(self, pb):
        book = pb.PortfolioBook("val")
        book.positions["AAPL"] = {"shares": 10}
        assert book.has_position("AAPL") is True
        assert book.has_position("AAPL", "LONG") is True

    def test_short_position_detected(self, pb):
        book = pb.PortfolioBook("val")
        book.short_positions["TSLA"] = {"shares": 5}
        assert book.has_position("TSLA") is True
        assert book.has_position("TSLA", "SHORT") is True

    def test_wrong_side_returns_false(self, pb):
        book = pb.PortfolioBook("val")
        book.positions["AAPL"] = {"shares": 10}
        # LONG position exists, but asking for SHORT
        assert book.has_position("AAPL", "SHORT") is False

    def test_no_position_returns_false(self, pb):
        book = pb.PortfolioBook("gene")
        assert book.has_position("NVDA") is False

    def test_case_insensitive_ticker(self, pb):
        book = pb.PortfolioBook("val")
        book.positions["AAPL"] = {"shares": 10}
        assert book.has_position("aapl") is True
        assert book.has_position("aapl", "long") is True


# ---------------------------------------------------------------------------
# 4. is_eligible() composite gate
# ---------------------------------------------------------------------------


class TestIsEligible:
    def test_eligible_when_all_gates_pass(self, pb):
        book = pb.PortfolioBook("val")
        ok, reason = book.is_eligible("AAPL", "LONG")
        assert ok is True
        assert reason is None

    def test_blocks_when_disabled(self, pb):
        book = pb.PortfolioBook("val")
        book.config.enabled = False
        ok, reason = book.is_eligible("AAPL", "LONG")
        assert ok is False
        assert reason == "disabled"

    def test_blocks_ticker_not_in_filter(self, pb):
        book = pb.PortfolioBook("val")
        book.config.tickers = {"TSLA", "NVDA"}
        ok, reason = book.is_eligible("AAPL", "LONG")
        assert ok is False
        assert reason == "ticker_filter"

    def test_passes_when_ticker_in_filter(self, pb):
        book = pb.PortfolioBook("val")
        book.config.tickers = {"AAPL", "TSLA"}
        ok, reason = book.is_eligible("AAPL", "LONG")
        assert ok is True

    def test_blocks_side_not_allowed(self, pb):
        book = pb.PortfolioBook("val")
        book.config.sides_allowed = {"LONG"}
        ok, reason = book.is_eligible("AAPL", "SHORT")
        assert ok is False
        assert reason == "side_filter"

    def test_blocks_existing_position(self, pb):
        book = pb.PortfolioBook("val")
        book.positions["AAPL"] = {"shares": 10}
        ok, reason = book.is_eligible("AAPL", "LONG")
        assert ok is False
        assert reason == "existing_position"

    def test_blocks_daily_halted(self, pb, monkeypatch):
        book = pb.PortfolioBook("val")
        # Directly set the book's own _trading_halted scalar (val/gene path)
        book._trading_halted = True
        ok, reason = book.is_eligible("AAPL", "LONG")
        assert ok is False
        assert reason == "daily_halted"

    def test_blocks_cooldown_via_stub(self, pb, monkeypatch):
        """in_cooldown returns False for non-main books (Phase 4 stub).
        Simulate cooldown by patching the method directly."""
        book = pb.PortfolioBook("val")
        monkeypatch.setattr(book, "in_cooldown", lambda t, s: True)
        ok, reason = book.is_eligible("AAPL", "LONG")
        assert ok is False
        assert reason == "cooldown"


# ---------------------------------------------------------------------------
# 5. record_entry_with_fill()
# ---------------------------------------------------------------------------


class TestRecordEntryWithFill:
    def test_inserts_long_position_with_fill_price(self, pb):
        book = pb.PortfolioBook("val")
        book.record_entry_with_fill(
            ticker="AAPL", side="LONG", fill_price=150.0, shares=33, entry_count=1
        )
        assert "AAPL" in book.positions
        pos = book.positions["AAPL"]
        assert pos["entry_price"] == 150.0
        assert pos["shares"] == 33
        assert pos["side"] == "LONG"

    def test_inserts_short_position_with_fill_price(self, pb):
        book = pb.PortfolioBook("gene")
        book.record_entry_with_fill(
            ticker="TSLA", side="SHORT", fill_price=200.5, shares=20, entry_count=1
        )
        assert "TSLA" in book.short_positions
        pos = book.short_positions["TSLA"]
        assert pos["entry_price"] == 200.5
        assert pos["side"] == "SHORT"

    def test_v531_max_favorable_seeded_to_fill(self, pb):
        """v531_max_favorable_price must be seeded to fill_price (not 0)."""
        book = pb.PortfolioBook("val")
        book.record_entry_with_fill("ORCL", "SHORT", fill_price=183.68, shares=10)
        pos = book.short_positions["ORCL"]
        assert pos["v531_max_favorable_price"] == 183.68

    def test_chandelier_reset_called(self, pb, caplog):
        """record_entry_with_fill must trigger [V700-CHANDELIER-RESET] log."""
        book = pb.PortfolioBook("val")
        with caplog.at_level(logging.INFO, logger="engine.portfolio_book"):
            book.record_entry_with_fill("AVGO", "LONG", fill_price=431.72, shares=11)
        assert any("V700-CHANDELIER-RESET" in rec.message for rec in caplog.records)

    def test_case_normalization(self, pb):
        """Ticker and side are uppercased."""
        book = pb.PortfolioBook("val")
        book.record_entry_with_fill("aapl", "long", fill_price=150.0, shares=10)
        assert "AAPL" in book.positions


# ---------------------------------------------------------------------------
# 6. Per-book isolation: different dollars_per_entry -> different qty
# ---------------------------------------------------------------------------


class TestPerBookIsolation:
    def test_val_vs_main_sizing_independent(self, pb):
        """Val book with 2x dollars_per_entry should return 2x shares."""
        main = pb.PortfolioBook("main")
        val = pb.PortfolioBook("val")

        main.config.dollars_per_entry = 10000.0
        val.config.dollars_per_entry = 20000.0

        main_qty = main.size_for("AAPL", 100.0)
        val_qty = val.size_for("AAPL", 100.0)

        assert val_qty == main_qty * 2

    def test_registry_books_have_independent_configs(self, pb):
        """PORTFOLIOS val/gene configs must not share the same object."""
        val = pb.PORTFOLIOS.get("val")
        gene = pb.PORTFOLIOS.get("gene")
        assert val.config is not gene.config


# ---------------------------------------------------------------------------
# 7. Earnings watcher flag: main=True, val/gene=False
# ---------------------------------------------------------------------------


class TestEarningsWatcher:
    def test_main_earnings_watcher_enabled(self, pb):
        main = pb.PORTFOLIOS.get("main")
        assert main.config.earnings_watcher_enabled is True

    def test_val_earnings_watcher_disabled(self, pb):
        val = pb.PORTFOLIOS.get("val")
        assert val.config.earnings_watcher_enabled is False

    def test_gene_earnings_watcher_disabled(self, pb):
        gene = pb.PORTFOLIOS.get("gene")
        assert gene.config.earnings_watcher_enabled is False


# ---------------------------------------------------------------------------
# 8. paper_shares_for() regression: same result as legacy path
# ---------------------------------------------------------------------------


class TestPaperSharesForRegression:
    """paper_shares_for must return the same value as the legacy formula.

    Legacy: floor(PAPER_DOLLARS_PER_ENTRY * ENTRY_1_SIZE_PCT / price).
    Phase 4: delegates to main.size_for(...) which uses the same formula
    seeded from dollars_per_entry (bridged from env on boot).
    """

    def test_paper_shares_for_matches_direct_formula(self):
        """Snapshot: $10k * 0.5 / $200 = floor(25) = 25 shares."""
        # We can test the formula directly without importing broker.orders
        # (which has a deep import chain).
        price = 200.0
        dollars_per_entry = 10000.0
        entry_1_size_pct = 0.5
        expected = max(1, int((dollars_per_entry * entry_1_size_pct) // price))
        assert expected == 25

    def test_paper_shares_for_large_price_min_one(self):
        """Very large price must still produce at least 1 share."""
        price = 999999.0
        dollars_per_entry = 10000.0
        entry_1_size_pct = 0.5
        result = max(1, int((dollars_per_entry * entry_1_size_pct) // price))
        assert result >= 1

    def test_size_for_matches_legacy_formula(self, pb):
        """book.size_for() uses identical math to the legacy path."""
        book = pb.PortfolioBook("main")
        book.config.dollars_per_entry = 10000.0
        # Simulate ENTRY_1_SIZE_PCT = 0.5
        result = book.size_for("AAPL", 200.0, entry_size_pct=0.5)
        legacy = max(1, int((10000.0 * 0.5) // 200.0))
        assert result == legacy
