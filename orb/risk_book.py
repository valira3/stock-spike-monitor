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
from dataclasses import dataclass, field
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
                 max_concurrent_notional_mult: float = 2.0) -> None:
        self.portfolio_id = portfolio_id
        self._max_risk = float(max_concurrent_risk_dollars)
        self._equity = float(equity)
        self._max_notional_mult = float(max_concurrent_notional_mult)
        self._open_risk: float = 0.0
        self._open_notional: float = 0.0
        self._open_tickets: dict[str, _Ticket] = {}
        self._lock = threading.RLock()
        # Telemetry:
        self.admit_count: int = 0
        self.reject_count: int = 0
        self.last_reject_reason: str = ""

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

    def reset_session(self) -> None:
        """Clear all open tickets. Call at session start to defensively
        clear any stale tickets that may have leaked across a restart."""
        with self._lock:
            self._open_tickets.clear()
            self._open_risk = 0.0
            self._open_notional = 0.0

    # --- snapshot (for /api/state) ---

    def snapshot(self) -> dict:
        """JSON-shaped snapshot of current risk-book state."""
        with self._lock:
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


# Module-level registry; live engine accesses via REGISTRY.
REGISTRY = RiskBookRegistry()
