"""engine/portfolio_book.py \u2014 v7.0.0 Phase 1.

Owns all per-book state for a single trading portfolio. Phase 1 keeps
ONE instance ('main') wired into trade_genius.py as a parallel container
that holds the same mutable references as the existing module-level
globals. No callsite changes are required in Phase 1.

Phase 3 will register Main + Val + Gene books behind the
PER_PORTFOLIO_BOOKS_ENABLED feature flag. Phase 2 will re-key cooldowns,
sentinels, and chandelier state by (portfolio_id, ticker, side).
"""

from __future__ import annotations


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
