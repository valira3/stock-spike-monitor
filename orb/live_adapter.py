"""orb.live_adapter -- bridges OrbEngine into the live scan-loop callback API.

The live engine (engine/scan.py:_per_ticker_tick) calls a per-tick
sequence:
  1. fetch_1min_bars(ticker) -- pull most recent bars
  2. archive_minute_bar(ticker, canon_bar) -- record forensics
  3. check_entry(ticker) -> (ok, bars_dict)
  4. if ok: execute_entry(ticker, price)
  (mirror for shorts)

PR4 (next) replaces (3) with a route through this adapter when
ORB_LIVE_MODE=1 (env-flag-gated, default on per the v10 keystone).

This module is the BRIDGE. It does not touch scan.py directly. It
provides a small, testable surface that scan.py can call:

  adapter = LiveAdapter(engine, portfolio_id="main")
  adapter.feed_bar(ticker, bucket_min, ohlc)
  result = adapter.check_entry(ticker, side="long",
                                five_min_close=..., next_open=...,
                                equity=...)
  # result = {"ok": bool, "side": ..., "shares": ..., "price": ...,
  #           "stop": ..., "target": ..., "ticket_id": ...}
  adapter.on_filled(ticker, ticket_id, fill_price, shares)
  adapter.check_exit(ticker, ticket_id, bar_high, bar_low, bar_close)

Multi-portfolio: one LiveAdapter per portfolio. Engine + day_gates are
shared; risk_book + day_states are per-portfolio inside the engine.

Look-ahead audit per rule #7b: feed_bar consults only the bar passed
in. check_entry uses only the locked OR window + the supplied 5m close
+ next-open price. No future data anywhere.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from orb import engine as _engine
from orb import exits as _exits
from orb import state as _state

logger = logging.getLogger(__name__)


@dataclass
class EntryResult:
    """Result of a check_entry / check_short_entry call."""
    ok: bool
    side: str = ""               # "long" or "short" or "" if not ok
    price: float = 0.0           # entry price (proposed_entry from signal)
    stop: float = 0.0
    target: float = 0.0
    shares: int = 0
    risk_dollars: float = 0.0
    ticket_id: str = ""
    reason_no: str = ""          # diagnostic when ok=False


@dataclass
class ExitResult:
    """Result of a check_exit call.

    v8.1.0 -- adds partial-fill fields. When `partial=True`, the
    position is HALF-CLOSED at `partial_price` and remains open with
    `remaining_shares`. Caller MUST:
      1. Submit a broker sell/buy for `partial_shares`.
      2. Do NOT remove the position from any open-position map.
      3. Continue calling check_exit on subsequent bars for the runner.
    `partial=True` and `exit=True` are mutually exclusive.
    """
    exit: bool
    reason: str = ""             # "target", "stop", "be_stop", "eod", "partial_1r", or ""
    price: float = 0.0
    # v8.1.0 partial-fill metadata. Populated when the engine emits
    # EXIT_PARTIAL and the adapter applies the half-close bookkeeping.
    partial: bool = False
    partial_shares: int = 0
    partial_price: float = 0.0
    remaining_shares: int = 0
    partial_pnl_dollars: float = 0.0


class LiveAdapter:
    """Per-portfolio bridge between scan.py callbacks and OrbEngine.

    Holds the OrbPosition objects keyed by (ticker, ticket_id) so the
    exit path can look them up without callers tracking the mapping.

    Thread-safety: scan.py runs the per-ticker loop on a single thread,
    so the adapter is single-threaded by design. The risk_book inside
    the engine has its own lock for cross-portfolio safety.
    """

    def __init__(self, engine: _engine.OrbEngine, portfolio_id: str) -> None:
        self.engine = engine
        self.portfolio_id = portfolio_id
        # ticket_id -> OrbPosition. Keyed on ticket so multiple positions
        # per ticker per day work (within max_trades_per_day cap).
        self._open_positions: dict[str, _exits.OrbPosition] = {}
        # ticker -> active ticket_id. Reverse lookup so the per-tick exit
        # path in broker/positions.py can find the v10 position state
        # given just a ticker. Note: at most one open v10 position per
        # (portfolio, ticker) per day under v10 semantics (max_trades is
        # serial, not parallel).
        self._ticker_to_ticket: dict[str, str] = {}
        # Per-ticker last seen 5-min close (for fast detect_breakout calls)
        self._last_5m_close: dict[str, float] = {}

    # --- session lifecycle ---

    def reset_session(self) -> None:
        """Clear adapter state on a new session. The OrbEngine is reset
        separately by the caller (typically once for all adapters).
        """
        self._open_positions.clear()
        self._ticker_to_ticket.clear()
        self._last_5m_close.clear()

    # --- bar feed ---

    def feed_bar(self, ticker: str, *,
                 bar_high: float, bar_low: float, bar_open: float,
                 bar_close: float, bar_volume: float,
                 bar_bucket_min: int) -> None:
        """Forward a 1-min bar to the engine's OR window.

        Idempotent: bars after OR lock are silently rejected by the
        OrWindow.add_bar() check.
        """
        self.engine.on_bar_arrival(
            ticker=ticker,
            bar_high=bar_high, bar_low=bar_low,
            bar_open=bar_open, bar_close=bar_close,
            bar_volume=bar_volume, bar_bucket_min=bar_bucket_min,
        )

    # --- entry decision ---

    def check_entry(self, ticker: str, *, side: str,
                    five_min_close: float, next_open: float,
                    equity: float, signal_iso: str = "",
                    recent_5m_highs: Optional[list[float]] = None,
                    recent_5m_lows: Optional[list[float]] = None,
                    recent_5m_closes: Optional[list[float]] = None,
                    session_vwap: Optional[float] = None,
                    ) -> EntryResult:
        """Single-side entry decision.

        Returns EntryResult.ok=True with full geometry if all of:
          1. Portfolio FSM is in armed/closed (can_enter)
          2. OR window is locked
          3. Detected a fresh breakout in the requested side
          4. v9 min_break_bps threshold met (if configured)
          5. v9 max_vwap_dev_bps not exceeded (if configured + fenced)
          6. Risk-book admits the proposed sizing

        Otherwise EntryResult.ok=False with reason_no diagnostic.

        v9.0.0: `session_vwap` is the cumulative session VWAP from
        session open through the signal bar's close. Caller (scan.py)
        passes it from the per-ticker accumulator; when None or zero,
        the vwap chase filter fails open (allows entry).
        """
        s = side.lower()
        if s not in ("long", "short"):
            return EntryResult(ok=False, reason_no=f"invalid_side:{side}")

        sig = self.engine.detect_breakout(
            portfolio_id=self.portfolio_id,
            ticker=ticker,
            five_min_close=five_min_close,
            five_min_close_iso=signal_iso,
            next_open=next_open,
            recent_5m_highs=recent_5m_highs,
            recent_5m_lows=recent_5m_lows,
            recent_5m_closes=recent_5m_closes,
        )
        if sig is None:
            return EntryResult(ok=False, reason_no="no_signal")
        if sig.side != s:
            return EntryResult(ok=False, reason_no=f"opposite_side:{sig.side}")

        # v9.0.0: snapshot pre-try_enter counters to detect chase
        # rejection (a None return from try_enter could be RiskBook OR
        # chase-filter; counters tell us which).
        # v9.1.7: time_cutoff counter added to the disambiguation set.
        mbr_before = self.engine._mbr_reject_count
        chase_before = self.engine._vwap_chase_reject_count
        cutoff_before = self.engine._time_cutoff_reject_count

        admission = self.engine.try_enter(
            sig, equity=equity, session_vwap=session_vwap,
        )
        if admission is None:
            # Disambiguate the rejection reason.
            if self.engine._time_cutoff_reject_count > cutoff_before:
                return EntryResult(ok=False, reason_no="time_cutoff")
            if self.engine._mbr_reject_count > mbr_before:
                return EntryResult(ok=False, reason_no="break_too_small")
            if self.engine._vwap_chase_reject_count > chase_before:
                return EntryResult(ok=False, reason_no="chase_too_far")
            rb = self.engine._risk.get(self.portfolio_id)
            reason = rb.last_reject_reason if rb else "no_risk_book"
            return EntryResult(ok=False, reason_no=f"risk_reject:{reason}")

        pos = admission.position
        self._open_positions[pos.risk_ticket_id] = pos
        self._ticker_to_ticket[ticker] = pos.risk_ticket_id
        return EntryResult(
            ok=True,
            side=s,
            price=pos.entry_price,
            stop=pos.stop,
            target=pos.target,
            shares=pos.shares,
            risk_dollars=pos.risk_dollars,
            ticket_id=pos.risk_ticket_id,
        )

    # --- exit decision ---

    def check_exit(self, ticker: str, ticket_id: str, *,
                   bar_high: float, bar_low: float, bar_close: float,
                   bar_bucket_min: int) -> ExitResult:
        """Per-bar exit evaluation for an open position.

        Returns ExitResult.exit=True with reason+price if the bar
        triggers an exit; otherwise exit=False.

        On exit, the position is REMOVED from the adapter's open map
        and the risk ticket is released.
        """
        pos = self._open_positions.get(ticket_id)
        if pos is None or pos.ticker != ticker:
            return ExitResult(exit=False, reason="unknown_position")

        decision = self.engine.evaluate_position_exit(
            pos,
            bar_high=bar_high, bar_low=bar_low, bar_close=bar_close,
            bar_bucket_min=bar_bucket_min,
        )
        if decision is None:
            return ExitResult(exit=False)

        # v8.1.0 -- partial-profit fire: half-close, position stays open
        # in the open map. Caller (scan.py / executor) submits the
        # broker partial-sell using the partial_shares + partial_price
        # surfaced on this ExitResult.
        from orb.exits import EXIT_PARTIAL
        if decision.reason == EXIT_PARTIAL:
            shares_closed, pnl = self.engine.on_partial_exit(
                pos, decision.price,
            )
            if shares_closed == 0:
                # Engine-side guard rejected the partial (already taken
                # or shares<2). Return no-op so caller doesn't fire a
                # broker order.
                return ExitResult(exit=False, reason="partial_noop")
            return ExitResult(
                exit=False, partial=True,
                reason=EXIT_PARTIAL,
                price=decision.price,
                partial_shares=shares_closed,
                partial_price=decision.price,
                remaining_shares=int(pos.shares),
                partial_pnl_dollars=float(pnl),
            )

        # Full exit: release ticket + drop from open map (existing path)
        self.engine.on_exit(pos, decision)
        self._open_positions.pop(ticket_id, None)
        # Drop from ticker map only if it points to this ticket (defensive
        # against a re-entry having overwritten the mapping in between)
        if self._ticker_to_ticket.get(ticker) == ticket_id:
            del self._ticker_to_ticket[ticker]
        return ExitResult(exit=True, reason=decision.reason, price=decision.price)

    def check_exit_by_ticker(self, ticker: str, *,
                             bar_high: float, bar_low: float,
                             bar_close: float,
                             bar_bucket_min: int) -> ExitResult:
        """Per-bar exit evaluation by ticker (no ticket_id needed).

        Convenience for callers (broker/positions.py:manage_positions)
        that don't track v10 ticket ids on their position dicts. Looks
        up the active ticket via the ticker_to_ticket reverse map.

        Returns ExitResult.exit=False with reason "no_open_v10_position"
        if there's no v10 position open for `ticker`. This is the
        common case under v10/legacy coexistence -- legacy positions
        still in tg.positions don't have a v10 ticket.
        """
        ticket_id = self._ticker_to_ticket.get(ticker)
        if ticket_id is None:
            return ExitResult(exit=False, reason="no_open_v10_position")
        return self.check_exit(
            ticker, ticket_id,
            bar_high=bar_high, bar_low=bar_low, bar_close=bar_close,
            bar_bucket_min=bar_bucket_min,
        )

    # --- introspection ---

    def open_position_count(self) -> int:
        return len(self._open_positions)

    def has_position(self, ticker: str) -> bool:
        return any(p.ticker == ticker for p in self._open_positions.values())

    def list_open_tickers(self) -> list[str]:
        return [p.ticker for p in self._open_positions.values()]


class LiveAdapterRegistry:
    """One adapter per portfolio_id. Live engine constructs at startup."""

    def __init__(self, engine: _engine.OrbEngine) -> None:
        self.engine = engine
        self._adapters: dict[str, LiveAdapter] = {}
        for pid in engine.portfolio_ids:
            self._adapters[pid] = LiveAdapter(engine, pid)

    def get(self, portfolio_id: str) -> Optional[LiveAdapter]:
        return self._adapters.get(portfolio_id)

    def all_ids(self) -> list[str]:
        return list(self._adapters.keys())

    def reset_all_sessions(self) -> None:
        for a in self._adapters.values():
            a.reset_session()
