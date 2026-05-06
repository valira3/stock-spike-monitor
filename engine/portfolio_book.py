"""engine/portfolio_book.py \u2014 v7.0.0 Phase 2.

Owns all per-book state for a single trading portfolio. Phase 1 kept
ONE instance ('main') wired into trade_genius.py as a parallel container
that holds the same mutable references as the existing module-level
globals. No callsite changes were required in Phase 1.

Phase 2 adds:
  - record_entry(): explicit chandelier reset on entry boundary
    (AVGO bug fix \u2014 spec section E). The method is the future-Phase-4
    anchor for broker-fill-price as the entry baseline; for Phase 2 it
    calls TrailState.fresh() and stamps a fresh state on the position.
  - paper_state.py now uses .clear() + .update() for v5 dicts so dict
    identity is preserved across load_paper_state() calls.

Phase 3 will register Main + Val + Gene books behind the
Phase 3 registers Main + Val + Gene as three PortfolioBook instances
inside a PortfolioRegistry. All three are unconditionally available at
import time. Main remains identity-bound to trade_genius module globals
so existing callers are unchanged. Val and Gene are dormant in-memory
containers; Phase 4 wires the fanout layer and executors.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# v7.0.0 Phase 3 -- canonical portfolio identifiers
# ---------------------------------------------------------------------------

PORTFOLIO_MAIN = "main"
PORTFOLIO_VAL = "val"
PORTFOLIO_GENE = "gene"

ALL_PORTFOLIO_IDS: tuple = (PORTFOLIO_MAIN, PORTFOLIO_VAL, PORTFOLIO_GENE)


class PortfolioBook:
    """Container for all per-book trading state.

    Phase 1 usage: one instance named 'main' is created in
    trade_genius.py. Its mutable attributes (dicts and lists) are bound
    to the SAME objects as the existing module-level globals, so any
    mutation through either path is immediately visible through the
    other.

    Scalar fields (paper_cash, _trading_halted, _trading_halted_reason,
    daily_entry_date, daily_short_entry_date) remain authoritative at the
    trade_genius module level for Phase 1. Phase 2 will consolidate those
    writes here.
    """

    def __init__(self, portfolio_id: str = "main") -> None:
        self.portfolio_id: str = portfolio_id

        # --- Mutable collections (Phase 1: identity-shared with module globals) ---
        self.positions: dict = {}
        self.short_positions: dict = {}

        self.daily_entry_count: dict = {}
        self.daily_short_entry_count: dict = {}

        self.paper_trades: list = []
        self.paper_all_trades: list = []

        self.trade_history: list = []
        self.short_trade_history: list = []

        self.v5_long_tracks: dict = {}
        self.v5_short_tracks: dict = {}
        self.v5_active_direction: dict = {}

        # --- Scalar fields (Phase 1: kept as module-level globals in trade_genius; ---
        # --- these mirror defaults; authoritative values live in trade_genius.py)  ---
        self.daily_entry_date: str = ""
        self.daily_short_entry_date: str = ""
        self.paper_cash: float = 0.0
        self._trading_halted: bool = False
        self._trading_halted_reason: str = ""

    # ------------------------------------------------------------------
    # Phase 2A: chandelier reset on entry boundary (AVGO bug fix).
    # Spec section E.
    # ------------------------------------------------------------------

    def record_entry(
        self,
        ticker: str,
        side: str,
        entry_price: float,
        entry_count: Optional[int] = None,
        fill_price: Optional[float] = None,
        **kwargs,
    ) -> None:
        """Reset all chandelier trail state for (ticker, side) on a fresh entry.

        Called immediately after a new position dict is installed in
        positions / short_positions. Stamps a fresh TrailState with
        peak_close seeded to entry_price so the trail never inherits a
        stale high-water mark from a prior leg on the same (ticker, side).

        Phase 2: uses entry_price as the trail baseline. Phase 4 will
        switch broker-fill-based books (val, gene) to fill_price instead.

        Args:
            ticker:      Ticker symbol (e.g. 'AVGO').
            side:        'LONG' or 'SHORT' (case-insensitive).
            entry_price: Price at which the new position was opened.
            entry_count: Entry number within the session (1, 2, 3, ...).
                         Logged in the reset audit line. None when unknown.
            fill_price:  Broker fill price (Phase 4 placeholder). Not used
                         in Phase 2.
            **kwargs:    Reserved for future per-phase extensions.
        """
        from engine.alarm_f_trail import STAGE_INACTIVE, TrailState

        ticker_u = str(ticker).upper()
        side_u = str(side).upper()

        # Locate the position dict (may be absent on a very first entry if
        # execute_breakout hasn't yet inserted it; record_entry is idempotent
        # and installs a fresh trail_state regardless).
        pos_dict = (
            self.short_positions if side_u == "SHORT" else self.positions
        )
        pos = pos_dict.get(ticker_u)

        # Capture old trail state values for the audit log line.
        old_state: Optional[TrailState] = None
        if pos is not None:
            old_state = pos.get("trail_state")

        old_peak = getattr(old_state, "peak_close", None) if old_state else None
        old_stage = getattr(old_state, "stage", None) if old_state else None

        # Build the fresh trail state seeded to entry_price.
        fresh = TrailState.fresh()
        fresh.peak_close = float(entry_price)

        # Stamp the fresh state onto the position dict if it exists.
        if pos is not None:
            pos["trail_state"] = fresh

        # --- Audit log line (V700-CHANDELIER-RESET) ---
        # Log even when old state is None (first-ever entry for this
        # ticker/side) so every entry is auditable.
        entry_tag = f"entry#{entry_count}" if entry_count is not None else "entry#?"
        old_peak_str = f"{old_peak:.2f}" if old_peak is not None else "None"
        new_peak_str = f"{float(entry_price):.2f}"
        old_stage_str = str(old_stage) if old_stage is not None else "None"
        new_stage_str = str(STAGE_INACTIVE)

        logger.info(
            "[V700-CHANDELIER-RESET] %s %s %s \u2014 peak_close %s -> %s, stage %s -> %s",
            ticker_u,
            side_u,
            entry_tag,
            old_peak_str,
            new_peak_str,
            old_stage_str,
            new_stage_str,
        )

        return fresh

# ---------------------------------------------------------------------------
# v7.0.0 Phase 3 -- PortfolioRegistry
#
# Holds all three PortfolioBook instances (main, val, gene).
# All books are unconditionally registered at import time.
# Main is identity-bound to trade_genius module globals in
# trade_genius.py so every existing callsite keeps working unchanged.
# Val and Gene are dormant in-memory containers until Phase 4 wires
# the fanout layer and their Alpaca executors.
# ---------------------------------------------------------------------------


class PortfolioRegistry:
    """Registry of PortfolioBook instances for the three active portfolios.

    Phase 3 usage: all three books (main, val, gene) are registered at
    module load. Only main has live state; val and gene hold empty
    in-memory dicts until Phase 4 activates the fanout layer.
    """

    def __init__(self) -> None:
        self._books: dict[str, PortfolioBook] = {}

    def register(self, portfolio_id: str) -> "PortfolioBook":
        """Create and register a PortfolioBook if not already present."""
        if portfolio_id not in self._books:
            self._books[portfolio_id] = PortfolioBook(portfolio_id=portfolio_id)
        return self._books[portfolio_id]

    def get(self, portfolio_id: str) -> "PortfolioBook":
        """Return the registered book for portfolio_id."""
        return self._books[portfolio_id]

    def all(self) -> "dict[str, PortfolioBook]":
        """Return a shallow copy of the id -> book mapping."""
        return dict(self._books)

    def main(self) -> "PortfolioBook":
        """Convenience accessor for the main portfolio book."""
        return self._books[PORTFOLIO_MAIN]


# Module-level singleton: three books registered unconditionally.
PORTFOLIOS: PortfolioRegistry = PortfolioRegistry()
PORTFOLIOS.register(PORTFOLIO_MAIN)
PORTFOLIOS.register(PORTFOLIO_VAL)
PORTFOLIOS.register(PORTFOLIO_GENE)
