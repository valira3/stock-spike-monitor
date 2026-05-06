"""engine/portfolio_book.py \u2014 v7.0.0 Phase 4.

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
# v7.0.0 Phase 4 -- PortfolioConfig dataclass
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field


@dataclass
class PortfolioConfig:
    """Per-book configuration for sizing, filtering, and feature flags.

    Each PortfolioBook carries one PortfolioConfig instance. Defaults
    represent a standard $100k equity floor, both sides allowed, and all
    tickers in universe. Main book overrides earnings_watcher_enabled=True
    after registry setup. Val/Gene leave it False until validated.
    """
    enabled: bool = True
    tickers: Optional[set] = None          # None = all tickers in universe
    sides_allowed: set = field(default_factory=lambda: {"LONG", "SHORT"})
    dollars_per_entry: float = 10000.0
    daily_loss_limit_dollars: float = 1000.0
    portfolio_equity_floor: float = 100000.0   # sizing reference, not enforced
    earnings_watcher_enabled: bool = False     # main overrides to True


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

        # --- v7.0.0 Phase 2.5: per-book session ratchet dicts (Eugene's rule) ---
        # Keys: (portfolio_id, ticker).  Cleared on EOD daily reset via paper_state.
        # prior_legs_max_high_long[key] = highest intra-leg high across all closed
        # LONG legs today for (portfolio_id, ticker).
        # prior_legs_min_low_short[key] = lowest intra-leg low across all closed
        # SHORT legs today for (portfolio_id, ticker).
        self.prior_legs_max_high_long: dict[tuple[str, str], float] = {}
        self.prior_legs_min_low_short: dict[tuple[str, str], float] = {}

        # --- Scalar fields (Phase 1: kept as module-level globals in trade_genius; ---
        # --- these mirror defaults; authoritative values live in trade_genius.py)  ---
        self.daily_entry_date: str = ""
        self.daily_short_entry_date: str = ""
        self.paper_cash: float = 0.0
        self._trading_halted: bool = False
        self._trading_halted_reason: str = ""

        # --- v7.0.0 Phase 4: per-book configuration ---
        self.config: PortfolioConfig = PortfolioConfig()

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

    # ------------------------------------------------------------------
    # v7.0.0 Phase 2.5: session ratchet \u2014 Eugene's re-entry HOD/LOD rule.
    # record_exit updates the ratchet after each leg closes.
    # re_entry_ratchet_ok gates a new Entry-1 attempt.
    # ------------------------------------------------------------------

    def record_exit(
        self,
        ticker: str,
        side: str,
        leg_high: Optional[float] = None,
        leg_low: Optional[float] = None,
    ) -> None:
        """Update the session ratchet after a leg closes.

        For LONG legs, track the running maximum of each leg's intra-bar high
        (via leg_high = v531_max_favorable_price on exit).  For SHORT legs,
        track the running minimum of each leg's intra-bar low.  Idempotent;
        no-ops gracefully when the relevant extreme is None.

        Args:
            ticker:   Ticker symbol (case-insensitive).
            side:     'LONG' or 'SHORT' (case-insensitive).
            leg_high: Intra-leg high for a LONG position (v531_max_favorable_price).
            leg_low:  Intra-leg low for a SHORT position (v531_max_favorable_price).
        """
        ticker_u = str(ticker).upper()
        side_u = str(side).upper()
        key = (self.portfolio_id, ticker_u)

        if side_u == "LONG" and leg_high is not None:
            prior = self.prior_legs_max_high_long.get(key)
            new = max(prior, float(leg_high)) if prior is not None else float(leg_high)
            self.prior_legs_max_high_long[key] = new
            logger.info(
                "[V700-RATCHET] %s LONG ratchet %.2f -> %.2f (leg_high=%.2f)",
                ticker_u,
                prior if prior is not None else float("nan"),
                new,
                float(leg_high),
            )
        elif side_u == "SHORT" and leg_low is not None:
            prior = self.prior_legs_min_low_short.get(key)
            new = min(prior, float(leg_low)) if prior is not None else float(leg_low)
            self.prior_legs_min_low_short[key] = new
            logger.info(
                "[V700-RATCHET] %s SHORT ratchet %.2f -> %.2f (leg_low=%.2f)",
                ticker_u,
                prior if prior is not None else float("nan"),
                new,
                float(leg_low),
            )
        # If the relevant extreme is None, no-op (defensive).

    def re_entry_ratchet_ok(
        self,
        ticker: str,
        side: str,
        current_high: Optional[float] = None,
        current_low: Optional[float] = None,
    ) -> tuple:
        """Check whether a new Entry-1 clears the session ratchet.

        For the first leg of the day (no prior ratchet), always returns
        (True, None) so standard NHOD/NLOD logic governs.  For subsequent
        legs the new extreme must push strictly past every prior leg's
        extreme: current_high > prior_max_high (LONG) or current_low <
        prior_min_low (SHORT).

        Returns:
            (True, None) if the gate passes or cannot be evaluated.
            (False, detail_str) if the gate rejects the entry.
        """
        ticker_u = str(ticker).upper()
        side_u = str(side).upper()
        key = (self.portfolio_id, ticker_u)

        if side_u == "LONG":
            ratchet = self.prior_legs_max_high_long.get(key)
            if ratchet is None:
                return (True, None)  # first leg \u2014 no prior ratchet
            if current_high is None:
                return (True, None)  # can't check; pass through defensively
            if float(current_high) > ratchet:
                return (True, None)
            return (
                False,
                "current_high=%.2f prior_max_high=%.2f" % (float(current_high), ratchet),
            )
        elif side_u == "SHORT":
            ratchet = self.prior_legs_min_low_short.get(key)
            if ratchet is None:
                return (True, None)  # first leg \u2014 no prior ratchet
            if current_low is None:
                return (True, None)  # can't check; pass through defensively
            if float(current_low) < ratchet:
                return (True, None)
            return (
                False,
                "current_low=%.2f prior_min_low=%.2f" % (float(current_low), ratchet),
            )
        # Unknown side \u2014 pass through defensively.
        return (True, None)

    # ------------------------------------------------------------------
    # v7.0.0 Phase 4 -- per-book sizing, eligibility gates, and
    # fill-price-based entry booking.
    # ------------------------------------------------------------------

    def size_for(self, ticker: str, price: float, *, entry_size_pct: float = 0.5) -> int:
        """Per-book share sizing. Uses dollars_per_entry * entry_size_pct / price.

        Mirrors paper_shares_for() but reads THIS book's config, not the
        global env. entry_size_pct default 0.5 = Entry-1 starter
        (Entry-2 tops up to 100%).

        Args:
            ticker:          Ticker symbol (unused in sizing math; reserved
                             for future per-ticker overrides).
            price:           Current price per share.
            entry_size_pct:  Fraction of dollars_per_entry to deploy.
                             Default 0.5 for Entry-1 starter lot.

        Returns:
            int: Share count, minimum 1.  Returns 0 for invalid price.
        """
        if price <= 0:
            return 0
        dollars = self.config.dollars_per_entry * entry_size_pct
        return max(1, int(dollars // price))

    def has_position(self, ticker: str, side: str | None = None) -> bool:
        """True if this book holds an open position on ticker.

        Args:
            ticker: Ticker symbol (case-insensitive).
            side:   Optional 'LONG' or 'SHORT'. When None, checks both sides.

        Returns:
            bool: True when any matching position exists.
        """
        t = ticker.upper()
        if side is None:
            return t in self.positions or t in self.short_positions
        s = side.upper()
        return (t in self.positions) if s == "LONG" else (t in self.short_positions)

    def in_cooldown(self, ticker: str, side: str) -> bool:
        """Cooldown gate per (ticker, side).

        Main book delegates to the existing cooldown registry in
        trade_genius. Val/Gene return False (stub) until their own
        cooldown registry is wired in v7.1. Phase 4 ships this as a
        no-op for non-main books.

        Args:
            ticker: Ticker symbol (case-insensitive).
            side:   'LONG' or 'SHORT' (case-insensitive).

        Returns:
            bool: True when the ticker/side pair is in post-loss cooldown.
        """
        if self.portfolio_id == "main":
            try:
                import trade_genius as tg
                return bool(tg.is_in_post_loss_cooldown(ticker, side))
            except Exception:
                return False
        return False

    def daily_halted(self) -> bool:
        """True if this book hit its daily_loss_limit_dollars.

        Main reads tg._trading_halted; val/gene track their own scalar
        (Phase 4 stub: False until their own halt logic is wired in v7.1).

        Returns:
            bool: True when trading is halted for this book today.
        """
        if self.portfolio_id == "main":
            try:
                import trade_genius as tg
                return bool(getattr(tg, "_trading_halted", False))
            except Exception:
                return False
        return self._trading_halted

    def is_eligible(self, ticker: str, side: str) -> tuple:
        """Composite entry gate per spec section B.

        Checks in order: enabled, ticker filter, side filter, no existing
        position, not in cooldown, not daily-halted.

        Args:
            ticker: Ticker symbol (case-insensitive).
            side:   'LONG' or 'SHORT' (case-insensitive).

        Returns:
            tuple: (True, None) when eligible;
                   (False, reason_str) when blocked.
        """
        t = ticker.upper()
        s = side.upper()
        if not self.config.enabled:
            return (False, "disabled")
        if self.config.tickers is not None and t not in self.config.tickers:
            return (False, "ticker_filter")
        if s not in self.config.sides_allowed:
            return (False, "side_filter")
        if self.has_position(t):
            return (False, "existing_position")
        if self.in_cooldown(t, s):
            return (False, "cooldown")
        if self.daily_halted():
            return (False, "daily_halted")
        return (True, None)

    def record_entry_with_fill(
        self,
        ticker: str,
        side: str,
        fill_price: float,
        shares: int,
        entry_count: int = 1,
    ) -> None:
        """Book an entry using broker fill price as the baseline.

        Phase 4: entry baseline = broker fill price (not engine intent).
        Writes to this book's positions/short_positions dict and calls
        record_entry() for the chandelier reset. Used by val/gene
        executors on confirmed Alpaca fill. Main book continues to use
        the existing execute_breakout path.

        This kills the ORCL slippage class of bug: Val's deep-stop keys
        off Val's actual fill, Main's keys off Main's.

        Args:
            ticker:      Ticker symbol (case-insensitive).
            side:        'LONG' or 'SHORT' (case-insensitive).
            fill_price:  Broker's confirmed fill price.
            shares:      Number of shares filled.
            entry_count: Entry number within the session (default 1).
                         Phase 4: scale-in still routes via main; v7.1
                         picks up per-book scale-in tracking.
        """
        t = ticker.upper()
        s = side.upper()
        pos_dict = self.short_positions if s == "SHORT" else self.positions
        pos = {
            "entry_price": float(fill_price),
            "shares": int(shares),
            "entry_count": int(entry_count),
            "side": s,
            "v531_max_favorable_price": float(fill_price),
            "v531_min_adverse_price": float(fill_price),
        }
        pos_dict[t] = pos
        # Re-use existing chandelier reset so trail state is initialised
        # from the confirmed fill price, not an engine-intent price.
        self.record_entry(
            ticker=t,
            side=s,
            entry_price=float(fill_price),
            entry_count=entry_count,
            fill_price=float(fill_price),
        )


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

# v7.0.0 Phase 4: main book is the only one with earnings watcher active.
# Val and gene leave earnings_watcher_enabled=False until validated.
PORTFOLIOS.get(PORTFOLIO_MAIN).config.earnings_watcher_enabled = True
