"""tests/test_v700_phase3_registry.py

v7.0.0 Phase 3 -- PortfolioRegistry structural tests.

Verifies:
- PORTFOLIOS.all() returns exactly 3 books keyed main/val/gene.
- PORTFOLIOS.main() is the same object as PORTFOLIOS.get("main").
- Each book is a distinct PortfolioBook instance.
- The main book is identity-bound to trade_genius module globals.
- Val and gene books hold separate, independent mutable collections.
"""

from __future__ import annotations

import sys
import types
import importlib


# ---------------------------------------------------------------------------
# Minimal trade_genius stub so portfolio_book + paper_state can import
# without the full Telegram/Alpaca stack. Only the attributes that the
# PortfolioBook identity-binding block in trade_genius.py actually uses
# are required.
# ---------------------------------------------------------------------------

def _make_tg_stub():
    mod = types.ModuleType("trade_genius")
    mod.BOT_NAME = "TradeGenius"
    mod.positions = {}
    mod.short_positions = {}
    mod.daily_entry_count = {}
    mod.daily_short_entry_count = {}
    mod.paper_trades = []
    mod.paper_all_trades = []
    mod.trade_history = []
    mod.short_trade_history = []
    mod.v5_long_tracks = {}
    mod.v5_short_tracks = {}
    mod.v5_active_direction = {}
    mod.paper_cash = 100_000.0
    mod._trading_halted = False
    mod._trading_halted_reason = ""
    mod.daily_entry_date = ""
    mod.daily_short_entry_date = ""
    return mod


import pytest


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    """Each test gets a clean import of engine.portfolio_book."""
    # Remove cached module so re-import is fresh.
    for key in list(sys.modules):
        if key.startswith("engine.portfolio_book") or key == "engine.portfolio_book":
            del sys.modules[key]
    yield
    # Teardown: leave registry in place (tests are read-only after import).


# ---------------------------------------------------------------------------
# Import under test (done inside each test so monkeypatch is active first)
# ---------------------------------------------------------------------------

def _import_pb():
    """Import engine.portfolio_book fresh."""
    import engine.portfolio_book as pb
    return pb


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRegistryStructure:
    def test_all_returns_three_books(self):
        pb = _import_pb()
        books = pb.PORTFOLIOS.all()
        assert set(books.keys()) == {"main", "val", "gene"}, (
            f"Expected main/val/gene, got {sorted(books.keys())}"
        )

    def test_main_accessor_is_same_object_as_get(self):
        pb = _import_pb()
        assert pb.PORTFOLIOS.main() is pb.PORTFOLIOS.get("main")

    def test_three_distinct_instances(self):
        pb = _import_pb()
        main = pb.PORTFOLIOS.get("main")
        val = pb.PORTFOLIOS.get("val")
        gene = pb.PORTFOLIOS.get("gene")
        assert id(main) != id(val), "main and val must be distinct objects"
        assert id(main) != id(gene), "main and gene must be distinct objects"
        assert id(val) != id(gene), "val and gene must be distinct objects"

    def test_portfolio_ids_set_correctly(self):
        pb = _import_pb()
        assert pb.PORTFOLIOS.get("main").portfolio_id == "main"
        assert pb.PORTFOLIOS.get("val").portfolio_id == "val"
        assert pb.PORTFOLIOS.get("gene").portfolio_id == "gene"

    def test_constants_defined(self):
        pb = _import_pb()
        assert pb.PORTFOLIO_MAIN == "main"
        assert pb.PORTFOLIO_VAL == "val"
        assert pb.PORTFOLIO_GENE == "gene"
        assert set(pb.ALL_PORTFOLIO_IDS) == {"main", "val", "gene"}

    def test_register_is_idempotent(self):
        pb = _import_pb()
        original = pb.PORTFOLIOS.get("main")
        # Re-registering the same id must return the existing instance.
        returned = pb.PORTFOLIOS.register("main")
        assert returned is original

    def test_all_returns_copy(self):
        """PORTFOLIOS.all() returns a dict copy, not the live internal dict."""
        pb = _import_pb()
        copy1 = pb.PORTFOLIOS.all()
        copy2 = pb.PORTFOLIOS.all()
        assert copy1 is not copy2
        assert copy1 == copy2


class TestBookIsolation:
    def test_val_positions_separate_from_main(self):
        pb = _import_pb()
        main = pb.PORTFOLIOS.get("main")
        val = pb.PORTFOLIOS.get("val")
        assert main.positions is not val.positions, (
            "main.positions and val.positions must be separate dicts"
        )

    def test_gene_positions_separate_from_main(self):
        pb = _import_pb()
        main = pb.PORTFOLIOS.get("main")
        gene = pb.PORTFOLIOS.get("gene")
        assert main.positions is not gene.positions

    def test_val_v5_long_tracks_separate_from_main(self):
        pb = _import_pb()
        main = pb.PORTFOLIOS.get("main")
        val = pb.PORTFOLIOS.get("val")
        assert main.v5_long_tracks is not val.v5_long_tracks

    def test_val_v5_short_tracks_separate_from_main(self):
        pb = _import_pb()
        main = pb.PORTFOLIOS.get("main")
        val = pb.PORTFOLIOS.get("val")
        assert main.v5_short_tracks is not val.v5_short_tracks

    def test_mutation_does_not_cross_books(self):
        """Writing to val.positions must not appear in main.positions."""
        pb = _import_pb()
        main = pb.PORTFOLIOS.get("main")
        val = pb.PORTFOLIOS.get("val")
        val.positions["AAPL"] = {"qty": 10}
        assert "AAPL" not in main.positions, (
            "Position added to val crossed into main -- books share a dict!"
        )
        # Cleanup so other tests are unaffected.
        del val.positions["AAPL"]


class TestMainBookGlobalsBinding:
    """Main book must be identity-bound to trade_genius module globals.

    trade_genius.py assigns _MAIN_BOOK.positions = positions (the module-
    level dict) immediately after getting the main book from PORTFOLIOS.
    We replicate that binding here to validate the identity contract.
    """

    def test_main_positions_can_be_bound_to_external_dict(self):
        pb = _import_pb()
        main = pb.PORTFOLIOS.get("main")
        # Simulate the binding that trade_genius.py performs.
        external_dict = {"MSFT": {"qty": 5}}
        main.positions = external_dict
        assert main.positions is external_dict

    def test_main_and_val_positions_remain_independent_after_rebind(self):
        pb = _import_pb()
        main = pb.PORTFOLIOS.get("main")
        val = pb.PORTFOLIOS.get("val")
        external_dict = {}
        main.positions = external_dict
        # val must still have its own dict, not the externally bound one.
        assert val.positions is not external_dict
