"""orb.risk_book -- per-portfolio concurrent-risk admission gate.

Live equivalent of the backtest's portfolio-level risk-budget gate
(tools/orb_backtest.py:run() lines 1318-1340). The backtest sorts events
chronologically and accepts greedily; live mode must check at the moment
of submission, atomically, with thread-safety.

One RiskBook instance per portfolio. Each enforces:
  - Concurrent open risk_dollars <= max_concurrent_risk_dollars
  - Concurrent open notional <= equity * max_concurrent_notional_mult
  - Single-trade notional <= equity * max_trade_notional_pct (informational;
    callers should also check this before calling try_admit)

API:
  rb = RiskBook(portfolio_id="main", max_concurrent_risk_dollars=2000.0,
                equity=100000.0, max_concurrent_notional_mult=2.0)
  ticket = rb.try_admit(risk_dollars=750.0, notional=15000.0)
  if ticket is None:
      # rejected -- log [ORB-RISK-REJECT] and don't submit broker order
  else:
      # accepted -- submit broker order, on fill or close call rb.release(ticket)
      rb.release(ticket)

Thread-safety: try_admit / release are guarded by an RLock.

Look-ahead audit per rule #7b: this module makes admission decisions
at submission time using only the current open-risk total. No future
data is consulted. The "at-time-of-submission" semantic is exactly
what the backtest models for live behavior.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Optional


@dataclass
class _Ticket:
    """Internal admission record. Returned by try_admit; passed to release."""
    ticket_id: str
    risk_dollars: float
    notional: float


class RiskBook:
    """Per-portfolio concurrent-risk admission gate.

    Independent across portfolios: Main's RiskBook is unaware of Val's
    open positions and vice versa. This matches the backtest semantics
    (each portfolio compounds independently with its own daily risk
    budget).

    Equity is settable so callers can refresh after compounding day-end
    or when a session-start broker query returns a new balance.
    """

    def __init__(self,
                 portfolio_id: str,
                 max_concurrent_risk_dollars: float = 2000.0,
                 equity: float = 100000.0,
                 max_concurrent_notional_mult: float = 2.0,
                 daily_loss_kill_pct: float = 2.0) -> None:
        self.portfolio_id = portfolio_id
        self._max_risk = float(max_concurrent_risk_dollars)
        self._equity = float(equity)
        self._max_notional_mult = float(max_concurrent_notional_mult)
        self._daily_loss_kill_pct = float(daily_loss_kill_pct)
        self._session_start_equity = float(equity)
        self._realized_pnl_today: float = 0.0
        self._open_risk: float = 0.0
        self._open_notional: float = 0.0
        self._open_tickets: dict[str, _Ticket] = {}
        self._lock = threading.RLock()
        # Telemetry:
        self.admit_count: int = 0
        self.reject_count: int = 0
        self.last_reject_reason: str = ""
        self.daily_kill_triggered: bool = False

    # --- properties ---

    @property
    def max_notional(self) -> float:
        return self._equity * self._max_notional_mult

    @property
    def max_risk_dollars(self) -> float:
        return self._max_risk

    @property
    def equity(self) -> float:
        return self._equity

    @property
    def open_risk(self) -> float:
        with self._lock:
            return self._open_risk

    @property
    def open_notional(self) -> float:
        with self._lock:
            return self._open_notional

    @property
    def open_count(self) -> int:
        with self._lock:
            return len(self._open_tickets)

    # --- equity refresh ---

    def update_equity(self, new_equity: float) -> None:
        """Refresh equity (for compounding on session start, or after a
        broker reconciliation). Concurrency-safe."""
        with self._lock:
            self._equity = float(new_equity)

    # --- v7.29.0: daily-loss kill accounting ---

    @property
    def realized_pnl_today(self) -> float:
        """Cumulative realized P&L on closed positions since the last
        session-start reset."""
        with self._lock:
            return self._realized_pnl_today

    @property
    def session_start_equity(self) -> float:
        """Equity at session start; used as the basis for the daily-loss
        kill threshold."""
        with self._lock:
            return self._session_start_equity

    @property
    def daily_kill_threshold_dollars(self) -> float:
        """Absolute realized-loss threshold above which entries are blocked.

        Returns a positive number: realized_pnl_today <= -threshold blocks.
        Computed as session_start_equity * daily_loss_kill_pct / 100.
        """
        with self._lock:
            return self._session_start_equity * self._daily_loss_kill_pct / 100.0

    def record_realized_pnl(self, pnl_dollars: float) -> bool:
        """Accumulate a realized P&L into today's running total.

        Returns True if THIS exit caused the daily-kill threshold to
        cross (was-above, is-below). Used by the engine to log the
        transition once.
        """
        with self._lock:
            was_killed = self.daily_kill_triggered
            self._realized_pnl_today += float(pnl_dollars)
            if not was_killed:
                threshold = self._session_start_equity * self._daily_loss_kill_pct / 100.0
                if threshold > 0 and self._realized_pnl_today <= -threshold:
                    self.daily_kill_triggered = True
                    return True
            return False

    def is_daily_killed(self) -> bool:
        """Cheap read of the kill state; used by try_admit + by the
        engine entry path."""
        with self._lock:
            return self.daily_kill_triggered

    # --- admission ---

    def try_admit(self,
                  risk_dollars: float,
                  notional: float,
                  reason_hint: str = "") -> Optional[_Ticket]:
        """Attempt to admit a new position. Returns a ticket on success,
        None on rejection.

        Atomically:
          1. If open_risk + risk_dollars > max_risk: REJECT (risk_cap)
          2. If open_notional + notional > max_notional: REJECT (notional_cap)
          3. Else: increment counters, create ticket, return it.

        Caller MUST call release(ticket) when the position closes. Failing
        to release will block future admissions. Use a try/finally pattern
        or wrap in a context manager (not provided here to keep deps
        minimal; positions module does the bookkeeping).
        """
        with self._lock:
            if risk_dollars < 0 or notional < 0:
                self.reject_count += 1
                self.last_reject_reason = "negative_size"
                return None
            # v7.29.0: daily-loss kill -- atomic check inside the same lock
            # so a concurrent record_realized_pnl can't sneak past.
            if self.daily_kill_triggered:
                self.reject_count += 1
                self.last_reject_reason = (
                    f"daily_kill (realized ${self._realized_pnl_today:.2f} "
                    f"<= -${self._session_start_equity * self._daily_loss_kill_pct / 100.0:.2f})"
                )
                return None
            new_risk = self._open_risk + risk_dollars
            new_notional = self._open_notional + notional
            if new_risk > self._max_risk + 0.005:  # tiny epsilon for fp noise
                self.reject_count += 1
                self.last_reject_reason = (
                    f"risk_cap (would-be ${new_risk:.2f} > ${self._max_risk:.2f})"
                )
                return None
            if new_notional > self.max_notional + 0.5:
                self.reject_count += 1
                self.last_reject_reason = (
                    f"notional_cap (would-be ${new_notional:.0f} > "
                    f"${self.max_notional:.0f})"
                )
                return None
            ticket = _Ticket(
                ticket_id=uuid.uuid4().hex,
                risk_dollars=float(risk_dollars),
                notional=float(notional),
            )
            self._open_tickets[ticket.ticket_id] = ticket
            self._open_risk = new_risk
            self._open_notional = new_notional
            self.admit_count += 1
            return ticket

    def release(self, ticket: _Ticket) -> bool:
        """Free the budget held by a ticket. Returns False if the ticket
        is unknown (defensive; should be impossible if callers respect
        the contract).

        Idempotent: a second release of the same ticket is a no-op
        returning True (already released).
        """
        if ticket is None:
            return False
        with self._lock:
            existing = self._open_tickets.pop(ticket.ticket_id, None)
            if existing is None:
                return False
            self._open_risk = max(0.0, self._open_risk - existing.risk_dollars)
            self._open_notional = max(0.0, self._open_notional - existing.notional)
            return True

    def release_by_id(self, ticket_id: str) -> bool:
        """v7.81.0 -- release a ticket by id when the caller doesn't
        hold the original ticket reference. Used by the v10 admit-
        rollback path in `orb.live_runtime.rollback_admit`.

        Returns True if a ticket was found and released, False otherwise.
        """
        if not ticket_id:
            return False
        with self._lock:
            existing = self._open_tickets.pop(str(ticket_id), None)
            if existing is None:
                return False
            self._open_risk = max(0.0, self._open_risk - existing.risk_dollars)
            self._open_notional = max(0.0, self._open_notional - existing.notional)
            return True

    # --- v7.105.0: disk persistence (Lesson 2) ---
    #
    # The RiskBook holds open ticket state that previously lived only
    # in process memory. Every Railway redeploy created a fresh
    # RiskBook with open_count=0 while tg.positions (which IS
    # persisted) reloaded from disk -- the exact mechanical root cause
    # of the phantom-position pattern across monitor issues
    # #532-#596. These helpers let paper_state.save_paper_state()
    # round-trip the open tickets through paper_state_main.json so the
    # post-deploy state mirrors the pre-deploy state.

    def serialize_tickets(self) -> list[dict]:
        """Return a JSON-serializable list of all currently-open tickets.

        Each ticket carries `ticket_id`, `risk_dollars`, `notional` --
        exactly the fields _Ticket holds. The aggregate `_open_risk`
        and `_open_notional` derived counters are NOT serialized
        separately because they're trivially re-derivable on restore.
        """
        with self._lock:
            return [
                {
                    "ticket_id": str(t.ticket_id),
                    "risk_dollars": float(t.risk_dollars),
                    "notional": float(t.notional),
                }
                for t in self._open_tickets.values()
            ]

    def restore_tickets(self, items: list[dict]) -> int:
        """Re-populate `_open_tickets` (and derived counters) from a
        previously-serialized list. Clears any existing tickets first
        -- restore is authoritative.

        Returns the number of tickets restored. Defensive against
        malformed input: any item that fails type coercion is skipped
        (logged at debug level by caller, this method stays silent).
        """
        with self._lock:
            self._open_tickets.clear()
            self._open_risk = 0.0
            self._open_notional = 0.0
            restored = 0
            for item in (items or []):
                if not isinstance(item, dict):
                    continue
                try:
                    tid = str(item.get("ticket_id") or "")
                    if not tid:
                        continue
                    risk = float(item.get("risk_dollars", 0.0) or 0.0)
                    notional = float(item.get("notional", 0.0) or 0.0)
                except (TypeError, ValueError):
                    continue
                if risk < 0 or notional < 0:
                    continue
                ticket = _Ticket(ticket_id=tid,
                                 risk_dollars=risk,
                                 notional=notional)
                self._open_tickets[tid] = ticket
                self._open_risk += risk
                self._open_notional += notional
                restored += 1
            return restored

    def reset_session(self) -> None:
        """Clear all open tickets. Call at session start to defensively
        clear any stale tickets that may have leaked across a restart.

        v7.29.0: also resets per-session realized P&L and the
        daily-kill flag, and snapshots session-start equity so the
        daily-loss threshold is computed against the open of the
        session (not against later MTM drift)."""
        with self._lock:
            self._open_tickets.clear()
            self._open_risk = 0.0
            self._open_notional = 0.0
            self._realized_pnl_today = 0.0
            self.daily_kill_triggered = False
            self._session_start_equity = self._equity

    # --- snapshot (for /api/state) ---

    def snapshot(self) -> dict:
        """JSON-shaped snapshot of current risk-book state."""
        with self._lock:
            kill_threshold = (
                self._session_start_equity * self._daily_loss_kill_pct / 100.0
            )
            return {
                "portfolio_id": self.portfolio_id,
                "equity": self._equity,
                "max_risk_dollars": self._max_risk,
                "max_notional": self.max_notional,
                "open_risk": self._open_risk,
                "open_notional": self._open_notional,
                "open_count": len(self._open_tickets),
                "admit_count": self.admit_count,
                "reject_count": self.reject_count,
                "last_reject_reason": self.last_reject_reason,
                "available_risk": max(0.0, self._max_risk - self._open_risk),
                "utilization_pct": (
                    100.0 * self._open_risk / self._max_risk
                    if self._max_risk > 0 else 0.0
                ),
                # v7.29.0: daily-loss kill telemetry
                "realized_pnl_today": self._realized_pnl_today,
                "session_start_equity": self._session_start_equity,
                "daily_kill_threshold": kill_threshold,
                "daily_kill_triggered": self.daily_kill_triggered,
                "daily_loss_kill_pct": self._daily_loss_kill_pct,
            }


class RiskBookRegistry:
    """One RiskBook per portfolio; lookup by portfolio_id.

    Live engine creates this once at startup and refreshes equity at
    session start. Dashboard reads via snapshot_all().
    """

    def __init__(self) -> None:
        self._books: dict[str, RiskBook] = {}
        self._lock = threading.RLock()

    def register(self, portfolio_id: str, **kwargs) -> RiskBook:
        """Create or replace the RiskBook for `portfolio_id`."""
        with self._lock:
            book = RiskBook(portfolio_id=portfolio_id, **kwargs)
            self._books[portfolio_id] = book
            return book

    def get(self, portfolio_id: str) -> Optional[RiskBook]:
        with self._lock:
            return self._books.get(portfolio_id)

    def all_ids(self) -> list[str]:
        with self._lock:
            return list(self._books.keys())

    def snapshot_all(self) -> dict[str, dict]:
        with self._lock:
            return {pid: rb.snapshot() for pid, rb in self._books.items()}

    def reset_all_sessions(self) -> None:
        with self._lock:
            for rb in self._books.values():
                rb.reset_session()

    # --- v7.105.0: registry-level disk persistence ---

    def serialize_all_tickets(self) -> dict[str, list[dict]]:
        """Return {portfolio_id: serialized_tickets} for every book.
        Empty dict when no books are registered (pre-bootstrap).
        """
        with self._lock:
            return {pid: rb.serialize_tickets() for pid, rb in self._books.items()}

    def restore_all_tickets(self, mapping: dict[str, list[dict]]) -> dict[str, int]:
        """Bulk-restore tickets for every portfolio_id in `mapping`.

        Only restores into books that ALREADY exist in the registry --
        if a portfolio_id has no book yet (e.g. the executor for that
        pid wasn't enabled at this boot), its tickets are silently
        dropped rather than auto-creating a phantom book. Returns
        {portfolio_id: count_restored} for each book actually touched.
        """
        out: dict[str, int] = {}
        if not isinstance(mapping, dict):
            return out
        with self._lock:
            for pid, items in mapping.items():
                book = self._books.get(pid)
                if book is None:
                    continue
                out[pid] = book.restore_tickets(items)
        return out


# Module-level registry; live engine accesses via REGISTRY.
REGISTRY = RiskBookRegistry()
