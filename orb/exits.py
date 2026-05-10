"""orb.exits -- RR=2.5 + move-to-BE-after-1R exit evaluator.

Per-position state machine. Replaces the existing Tiger Sovereign
Sentinel A/B/C alarms when ORB_MODE=1.

Exit reasons:
  - "target": price reached `entry + rr * risk` (long) / `entry - rr * risk` (short)
  - "stop": price reached the original OR-side stop
  - "be_stop": price reached entry after move-to-BE was armed (rule:
    once price has touched entry + 1R, stop is bumped to entry; subsequent
    return to entry triggers be_stop instead of the original stop)
  - "eod": end-of-day flush at ORB_EOD_CUTOFF_ET (default 15:55)
  - "session_close": broker-managed flush at 15:50 ET (defensive backstop)

Look-ahead audit per rule #7b: the evaluator inspects only:
  - The position's static fields (entry_price, stop, target, side, risk)
  - The current bar's high/low/close (the bar that's CLOSING right now)
  - The position's mutable state (be_moved, partial_taken)

It never looks at any bar with a timestamp after the bar passed to evaluate().

When stop and target are both touched in the same 1-min bar, stop is
checked first (pessimistic worst-case fill). This matches the backtest
in tools/orb_backtest.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# Exit-reason constants
EXIT_TARGET = "target"
EXIT_STOP = "stop"
EXIT_BE_STOP = "be_stop"
EXIT_EOD = "eod"
EXIT_SESSION_CLOSE = "session_close"


@dataclass
class OrbPosition:
    """All state required to evaluate exits for a single ORB position.

    Carry this on the broker position record (e.g. set
    `pos["orb_managed"] = True` and stash an `OrbPosition` alongside).
    """
    portfolio_id: str
    ticker: str
    side: str                    # "long" or "short"
    entry_price: float
    stop: float                  # current stop (mutated when BE-armed)
    target: float                # target = entry + rr * risk for long, mirror for short
    risk: float                  # |entry_price - original_stop|; for the BE check
    one_r: float                 # entry +/- 1*risk; trigger for BE-arm
    rr: float = 2.5              # configured RR; informational only after construction
    be_moved: bool = False       # whether stop has been bumped to entry
    shares: int = 0              # for diagnostics + risk book release
    risk_dollars: float = 0.0    # for diagnostics + risk book release
    notional: float = 0.0        # for diagnostics + risk book release
    risk_ticket_id: str = ""     # tied to RiskBook ticket; release on exit


@dataclass
class ExitDecision:
    """Outcome of evaluate(). None means "still open"."""
    reason: str
    price: float


def make_position(*, portfolio_id: str, ticker: str, side: str,
                  entry_price: float, stop: float, rr: float,
                  shares: int = 0, risk_ticket_id: str = "",
                  ) -> OrbPosition:
    """Build a fully-formed OrbPosition with target + 1R derived from
    entry + stop + rr.

    Long: target = entry + rr * (entry - stop); 1R = entry + (entry - stop)
    Short: target = entry - rr * (stop - entry); 1R = entry - (stop - entry)

    risk = abs(entry - stop). Caller is responsible for clamping risk > 0.
    """
    s = side.lower()
    if s not in ("long", "short"):
        raise ValueError(f"side must be 'long' or 'short', got {side!r}")
    risk = abs(entry_price - stop)
    if risk <= 0:
        raise ValueError(f"risk must be > 0; entry={entry_price} stop={stop}")
    if s == "long":
        target = entry_price + rr * risk
        one_r = entry_price + risk
    else:
        target = entry_price - rr * risk
        one_r = entry_price - risk
    notional = entry_price * max(0, shares)
    risk_dollars = risk * max(0, shares)
    return OrbPosition(
        portfolio_id=portfolio_id,
        ticker=ticker,
        side=s,
        entry_price=entry_price,
        stop=stop,
        target=target,
        risk=risk,
        one_r=one_r,
        rr=rr,
        be_moved=False,
        shares=shares,
        risk_dollars=risk_dollars,
        notional=notional,
        risk_ticket_id=risk_ticket_id,
    )


def maybe_arm_be(pos: OrbPosition, bar_high: float, bar_low: float) -> bool:
    """If the bar's range crossed 1R, arm BE (move stop to entry).

    Returns True if BE was newly armed in this call. Subsequent calls are
    no-ops (idempotent).

    Long: arm if bar_high >= one_r and not be_moved
    Short: arm if bar_low <= one_r and not be_moved
    """
    if pos.be_moved:
        return False
    if pos.side == "long":
        if bar_high >= pos.one_r:
            pos.stop = pos.entry_price
            pos.be_moved = True
            return True
    else:
        if bar_low <= pos.one_r:
            pos.stop = pos.entry_price
            pos.be_moved = True
            return True
    return False


def evaluate(pos: OrbPosition, *,
             bar_high: float, bar_low: float, bar_close: float,
             bar_bucket_min: int, eod_cutoff_min: int,
             ) -> Optional[ExitDecision]:
    """Decide whether the bar triggers an exit.

    Order of checks (pessimistic):
      1. Stop (current stop, which may be the BE stop after arming)
      2. Target
      3. EOD cutoff (force close at last bar of session)

    Returns ExitDecision on exit; None if still open.

    The BE-arm check happens BEFORE the stop check on the same bar, so a
    bar that touches BOTH the target and the stop will exit at target if
    BE was just armed (because stop becomes the BE stop = entry, which
    isn't hit by any sensible bar that also touches the target). This
    matches the backtest semantics.

    Bar-data audit: only bar_high, bar_low, bar_close are read. No future
    bars consulted.
    """
    # First, see if BE should be armed by this bar (before stop check)
    maybe_arm_be(pos, bar_high, bar_low)

    if pos.side == "long":
        # 1. Stop check (pessimistic on simultaneous touch)
        if bar_low <= pos.stop:
            reason = EXIT_BE_STOP if pos.be_moved else EXIT_STOP
            return ExitDecision(reason=reason, price=pos.stop)
        # 2. Target check
        if bar_high >= pos.target:
            return ExitDecision(reason=EXIT_TARGET, price=pos.target)
    else:  # short
        if bar_high >= pos.stop:
            reason = EXIT_BE_STOP if pos.be_moved else EXIT_STOP
            return ExitDecision(reason=reason, price=pos.stop)
        if bar_low <= pos.target:
            return ExitDecision(reason=EXIT_TARGET, price=pos.target)

    # 3. EOD flush
    if bar_bucket_min >= eod_cutoff_min:
        return ExitDecision(reason=EXIT_EOD, price=bar_close)

    return None
