"""orb.state -- v10 per-portfolio per-ticker FSM + market-wide OR window.

Two layers of state:

1. MARKET-WIDE: OrWindow per ticker. Accumulates 1-min bars during
   [09:30, 09:30 + or_minutes), locks at OR_END. Shared across all
   portfolios because the OR is a market property, not a portfolio
   property.

2. PER-PORTFOLIO: TickerDayState per (portfolio_id, ticker). Holds the
   FSM phase, today's trade count, current in-position flag, last
   signal bucket. Each portfolio can be in a different phase for the
   same ticker (e.g. main is in_pos on AAPL, val and gene are armed).

FSM phases:

    warmup
      |  bar.bucket >= or_end
      v
    or_locked
      |  evaluate_ticker_gates pass / fail
      v
    {armed | blocked_vix | blocked_earnings | blocked_gap |
     blocked_range | blocked_blocklist}
      |  signal fires (5m close past OR)
      v
    in_pos
      |  exit (target / stop / be_stop / eod)
      v
    closed
      |  trades_today < cap and another signal fires
      v
    armed (loop)

The session_date is reset at each new trading day. All windows + states
clear; a stale state never leaks across days.

Look-ahead audit per rule #7b: OrWindow.add_bar() only adds a bar to
the window if the bar's bucket is strictly inside [09:30, or_end).
locked() freezes the high/low at exactly or_end. No future bars
contribute. The FSM never reads any data with a timestamp greater than
the current bar; phase transitions are driven by events that have
already happened (bar close, signal fire, fill confirmation).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# Phase constants. String-typed so they appear cleanly in JSON snapshots.
PHASE_WARMUP = "warmup"
PHASE_OR_LOCKED = "or_locked"
PHASE_ARMED = "armed"
PHASE_IN_POS = "in_pos"
PHASE_CLOSED = "closed"
# Block phases (terminal-for-the-day on this ticker for this portfolio):
PHASE_BLOCKED_VIX = "blocked_vix"
PHASE_BLOCKED_EARNINGS = "blocked_earnings"
PHASE_BLOCKED_GAP = "blocked_gap"
PHASE_BLOCKED_RANGE = "blocked_range"
PHASE_BLOCKED_BLOCKLIST = "blocked_blocklist"
PHASE_BLOCKED_OR_INSUFFICIENT = "blocked_or_insufficient"
PHASE_BLOCKED_DAILY_KILL = "blocked_daily_kill"

ALL_BLOCKED_PHASES = frozenset({
    PHASE_BLOCKED_VIX,
    PHASE_BLOCKED_EARNINGS,
    PHASE_BLOCKED_GAP,
    PHASE_BLOCKED_RANGE,
    PHASE_BLOCKED_BLOCKLIST,
    PHASE_BLOCKED_OR_INSUFFICIENT,
    PHASE_BLOCKED_DAILY_KILL,
})


@dataclass
class OrWindow:
    """Market-wide OR window state for a single ticker.

    Built incrementally as 1-min bars arrive during [09:30, or_end).
    locked at or_end; high/low/open/close are final from that point.

    Attributes:
        ticker: symbol, e.g. "AAPL"
        or_minutes: window size, typically 30
        or_high: max(bar.high) across all bars in the window
        or_low: min(bar.low) across all bars in the window
        or_open: open of the first bar in the window (09:30)
        or_close: close of the last bar in the window (or_end - 1min)
        or_volume: sum of bar volumes in the window
        bars_seen: count of bars contributed to the window
        locked: True once the window has been frozen at or_end
        locked_at_iso: ISO timestamp of lock, for forensic logs
    """
    ticker: str
    or_minutes: int = 30
    or_high: Optional[float] = None
    or_low: Optional[float] = None
    or_open: Optional[float] = None
    or_close: Optional[float] = None
    or_volume: float = 0.0
    bars_seen: int = 0
    locked: bool = False
    locked_at_iso: Optional[str] = None

    @property
    def or_width_pct(self) -> Optional[float]:
        """Width as a fraction of midpoint, e.g. 0.012 = 1.2%."""
        if not self.locked or self.or_high is None or self.or_low is None:
            return None
        mid = (self.or_high + self.or_low) / 2.0
        if mid <= 0:
            return None
        return (self.or_high - self.or_low) / mid

    def add_bar(self, bar_high: float, bar_low: float, bar_open: float,
                bar_close: float, bar_volume: float, bar_bucket_min: int,
                or_end_min: int) -> bool:
        """Incorporate a 1-min bar if it falls strictly inside the OR window.

        Returns True if the bar was added; False if it was outside the
        window or the window is already locked.

        Look-ahead clean: only bars with bucket < or_end are accepted.
        Bars at or after or_end are rejected (they belong to post-OR).
        """
        if self.locked:
            return False
        # Window is [09:30, 09:30 + or_minutes); start_min derived from
        # or_end_min - or_minutes by the caller.
        if bar_bucket_min >= or_end_min:
            return False
        if self.or_open is None:
            self.or_open = bar_open
        self.or_high = bar_high if self.or_high is None else max(self.or_high, bar_high)
        self.or_low = bar_low if self.or_low is None else min(self.or_low, bar_low)
        self.or_close = bar_close
        self.or_volume += bar_volume
        self.bars_seen += 1
        return True

    def lock(self, locked_at_iso: str) -> None:
        """Freeze the window. Subsequent add_bar() calls return False.

        Caller invokes this at exactly or_end (no earlier, no later).
        """
        self.locked = True
        self.locked_at_iso = locked_at_iso


@dataclass
class TickerDayState:
    """Per-portfolio per-ticker FSM state for a single trading day.

    Reset at each new session. Independent across portfolios: portfolio
    Main can be in_pos on AAPL while Val is armed and Gene is closed.
    """
    portfolio_id: str
    ticker: str
    phase: str = PHASE_WARMUP
    block_reason: str = ""           # human-readable string for diagnostics
    trades_today: int = 0
    in_position: bool = False
    last_signal_bucket: Optional[int] = None
    last_entry_iso: Optional[str] = None
    last_exit_iso: Optional[str] = None
    consecutive_losses: int = 0      # informational; not gating logic

    def is_blocked(self) -> bool:
        return self.phase in ALL_BLOCKED_PHASES

    def can_enter(self, max_trades_per_day: int) -> bool:
        """True iff a new entry is admissible right now."""
        if self.is_blocked():
            return False
        if self.in_position:
            return False
        if self.trades_today >= max_trades_per_day:
            return False
        return self.phase in (PHASE_ARMED, PHASE_CLOSED)

    def transition(self, new_phase: str, reason: str = "") -> None:
        """Move to a new phase. Sets block_reason for blocked transitions."""
        self.phase = new_phase
        if new_phase in ALL_BLOCKED_PHASES:
            self.block_reason = reason or new_phase
        else:
            self.block_reason = ""


class OrbStateRegistry:
    """Owner of all market-wide OR windows + per-portfolio FSM states.

    Single instance per process. Methods are NOT thread-safe by default;
    callers that mutate from multiple threads must lock externally. The
    live engine mutates from the per-cycle scan thread (single-threaded
    by design), so this is fine.

    Multi-portfolio shape: one OR window per ticker is shared across
    all portfolios. Each (portfolio_id, ticker) has its own
    TickerDayState.
    """

    def __init__(self) -> None:
        self.or_windows: dict[str, OrWindow] = {}
        self.day_states: dict[tuple[str, str], TickerDayState] = {}
        self.session_date: str = ""

    # --- session lifecycle ---

    def reset_for_new_session(self, date_iso: str) -> None:
        """Clear all OR windows + FSM states for a new trading day.

        Call at the first bar of the new session (or via scheduler at
        09:00 ET to be safe). Idempotent if called with the same date
        repeatedly within one session.
        """
        if self.session_date == date_iso:
            return
        self.or_windows.clear()
        self.day_states.clear()
        self.session_date = date_iso

    # --- OR window access ---

    def get_or_window(self, ticker: str, or_minutes: int) -> OrWindow:
        """Get or create the OR window for `ticker`. Creation is lazy so
        a ticker that never trades doesn't carry an empty window."""
        w = self.or_windows.get(ticker)
        if w is None:
            w = OrWindow(ticker=ticker, or_minutes=or_minutes)
            self.or_windows[ticker] = w
        return w

    def lock_all_or_windows(self, locked_at_iso: str) -> None:
        """Lock every existing OR window. Called at or_end (e.g. 10:00)."""
        for w in self.or_windows.values():
            if not w.locked:
                w.lock(locked_at_iso)

    # --- per-portfolio per-ticker FSM access ---

    def get_day_state(self, portfolio_id: str, ticker: str) -> TickerDayState:
        """Get or create the per-portfolio per-ticker FSM state."""
        key = (portfolio_id, ticker)
        s = self.day_states.get(key)
        if s is None:
            s = TickerDayState(portfolio_id=portfolio_id, ticker=ticker)
            self.day_states[key] = s
        return s

    # --- snapshot helpers (for dashboard /api/state) ---

    def snapshot_or_windows(self) -> dict[str, dict]:
        """Return a JSON-shaped dict of all OR windows for the dashboard."""
        out: dict[str, dict] = {}
        for ticker, w in self.or_windows.items():
            out[ticker] = {
                "or_high": w.or_high,
                "or_low": w.or_low,
                "or_open": w.or_open,
                "or_close": w.or_close,
                "or_volume": w.or_volume,
                "or_width_pct": w.or_width_pct,
                "bars_seen": w.bars_seen,
                "locked": w.locked,
                "locked_at_iso": w.locked_at_iso,
            }
        return out

    def snapshot_day_states(self) -> list[dict]:
        """Return a JSON-shaped list of all (portfolio, ticker) FSM states."""
        out: list[dict] = []
        for (portfolio_id, ticker), s in self.day_states.items():
            out.append({
                "portfolio_id": portfolio_id,
                "ticker": ticker,
                "phase": s.phase,
                "block_reason": s.block_reason,
                "trades_today": s.trades_today,
                "in_position": s.in_position,
                "last_signal_bucket": s.last_signal_bucket,
                "last_entry_iso": s.last_entry_iso,
                "last_exit_iso": s.last_exit_iso,
            })
        return out


# Module-level singleton. Live engine accesses via REGISTRY.
# Tests: construct a fresh OrbStateRegistry() per test for isolation.
REGISTRY = OrbStateRegistry()
