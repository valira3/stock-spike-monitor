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
# v8.1.0 -- partial-profit-at-1R fire. Caller must:
#   1. submit a broker sell for `position.shares // 2` shares;
#   2. mutate position.shares to the remainder, set partial_taken=True
#      and record partial_pnl_dollars on the position;
#   3. RELEASE proportional risk-book budget (not the full ticket);
#   4. continue evaluating the remaining position on subsequent bars.
# Unlike target/stop/eod/be_stop, EXIT_PARTIAL does NOT close the
# position -- it half-closes it. The caller must NOT call on_exit() on
# this decision; instead, see engine.on_partial_exit().
EXIT_PARTIAL = "partial_1r"
# R21 (v9.1.x) -- runner_eod_prep. Fires AFTER partial-at-1R has been
# taken (pos.partial_taken=True) and bar_bucket_min crosses the
# runner_eod_prep_minutes threshold. Used to capture the runner half
# at a local-max-proxy time (default 14:00 ET) before the afternoon
# giveback into 15:55 EOD. Distinct from EXIT_EOD which is the
# whole-session forced flush.
EXIT_RUNNER_EOD_PREP = "runner_eod_prep"
# R26 (v9.1.130) -- stale_full_exit. Mirror of EXIT_RUNNER_EOD_PREP but
# for the un-partialed cohort: fires when partial_taken=False AND
# bar_bucket_min >= stale_full_exit_min. Optional MFE-in-R floor spares
# trades that came close to 1R (floor=0 = always fire at cutoff). Catches
# afternoon-driftback losses on positions that never hit 1R -- the
# legacy sentinel A safety net that v9.1.128's portfolio independence
# removed for Val/Gene.
EXIT_STALE_FULL_EXIT = "stale_full_exit"


@dataclass
class OrbPosition:
    """All state required to evaluate exits for a single ORB position.

    Carry this on the broker position record (e.g. set
    `pos["orb_managed"] = True` and stash an `OrbPosition` alongside).
    """

    portfolio_id: str
    ticker: str
    side: str  # "long" or "short"
    entry_price: float
    stop: float  # current stop (mutated when BE-armed)
    target: float  # target = entry + rr * risk for long, mirror for short
    risk: float  # |entry_price - original_stop|; for the BE check
    one_r: float  # entry +/- 1*risk; trigger for BE-arm
    rr: float = 2.5  # configured RR; informational only after construction
    be_moved: bool = False  # whether stop has been bumped to entry
    shares: int = 0  # CURRENT open shares (mutated by partial fill)
    risk_dollars: float = 0.0  # for diagnostics + risk book release
    notional: float = 0.0  # for diagnostics + risk book release
    risk_ticket_id: str = ""  # tied to RiskBook ticket; release on exit
    # v8.1.0 -- partial-profit-at-1R state. partial_taken flips True the
    # first time the engine emits EXIT_PARTIAL for this position and the
    # caller acts on it. partial_pnl_dollars records the realized P&L of
    # the half closed at 1R (always >= 0 since 1R is profit by definition).
    # original_shares is the pre-partial count, preserved for forensic
    # logging and percent-position-remaining math.
    partial_taken: bool = False
    partial_pnl_dollars: float = 0.0
    original_shares: int = 0
    # R26 (v9.1.130) -- MFE tracking for the stale_full_exit lever's
    # optional MFE-in-R floor. Updated each evaluate() call before the
    # exit-decision checks. Initialized to entry_price; the first
    # evaluate() bar updates it via bar_high (long) / bar_low (short).
    mfe_price: float = 0.0


@dataclass
class ExitDecision:
    """Outcome of evaluate(). None means "still open"."""

    reason: str
    price: float


def make_position(
    *,
    portfolio_id: str,
    ticker: str,
    side: str,
    entry_price: float,
    stop: float,
    rr: float,
    shares: int = 0,
    risk_ticket_id: str = "",
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
        # v8.1.0 -- partial state begins false; original_shares mirrors
        # initial shares so post-partial code can reason about the
        # original sizing.
        partial_taken=False,
        partial_pnl_dollars=0.0,
        original_shares=int(max(0, shares)),
        # R26 -- MFE starts at entry; will move favorably on later bars.
        mfe_price=entry_price,
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


def evaluate(
    pos: OrbPosition,
    *,
    bar_high: float,
    bar_low: float,
    bar_close: float,
    bar_bucket_min: int,
    eod_cutoff_min: int,
    partial_profit_at_1r: bool = False,
    runner_eod_prep_min: int = 0,
    stale_full_exit_min: int = 0,
    stale_full_exit_mfe_floor_r: float = 0.0,
) -> Optional[ExitDecision]:
    """Decide whether the bar triggers an exit OR a partial-profit fill.

    Order of checks (pessimistic):
      0. Partial-profit at 1R (v8.1.0; only if `partial_profit_at_1r=True`
         AND `pos.partial_taken` is False AND `pos.shares >= 2`).
      1. Stop (current stop, which may be the BE stop after arming)
      2. Target
      3. EOD cutoff (force close at last bar of session)

    Returns ExitDecision on exit; None if still open.

    On a same-bar 1R + stop touch:
      - The partial-fire check happens BEFORE the stop check, so the
        half-close at 1R books profit AND the BE-arm hands the runner
        a stop at entry. A subsequent bar that pierces entry triggers
        be_stop on the remaining half. This matches backtest order.
      - In the rare same-bar 1R + target case (only possible if RR <= 1,
        which we don't trade), partial would fire first and the runner
        would exit at target on the very same bar. Caller is expected
        to handle the back-to-back exits.

    The caller's contract on EXIT_PARTIAL:
      - Submit broker sell/buy for `pos.shares // 2`.
      - Mutate the position: set partial_taken=True, set
        partial_pnl_dollars = (one_r - entry_price) * half (long) or
        (entry_price - one_r) * half (short), reduce pos.shares by half.
      - Release proportional risk-book budget.
      - Re-call evaluate() on the SAME bar's exit side -- in case the
        bar also pierced the stop or target (rare but possible on
        explosive moves). evaluate() will return None if the remaining
        position's stop/target weren't touched by this bar.

    Bar-data audit: only bar_high, bar_low, bar_close are read. No future
    bars consulted.
    """
    # Order preserves the pre-v8.1.0 live-engine semantics:
    #   1. BE arm (existing, BEFORE stop check -- optimistic on a same-
    #      bar 1R+stop touch, exits at the new BE stop rather than the
    #      original).
    #   2. Partial fire (v8.1.0, gated on partial_profit_at_1r AND 1R
    #      touch AND not yet taken AND shares >= 2). Fires AFTER BE
    #      arm so the runner inherits stop=entry on the same bar.
    #   3. Stop check (using current stop, which is BE if just armed).
    #   4. Target check.
    #   5. EOD.
    # Divergence from backtest: backtest checks stop FIRST so a
    # same-bar 1R+stop pierce exits at the original stop. The
    # pre-v8.1.0 live engine deliberately chose the optimistic
    # BE-first ordering; v8.1.0 preserves that and slots partial in
    # AFTER BE-arm. Live therefore OUTPERFORMS backtest by a small
    # margin on stop-pierce-then-recover days.
    # R26 -- update MFE tracking BEFORE any exit check so the floor
    # comparison uses this bar's high (long) / low (short). Idempotent
    # if pos.mfe_price was initialized to entry_price by make_position.
    if pos.side == "long":
        if bar_high > pos.mfe_price:
            pos.mfe_price = bar_high
    else:
        if bar_low < pos.mfe_price or pos.mfe_price == 0.0:
            pos.mfe_price = bar_low

    maybe_arm_be(pos, bar_high, bar_low)

    # v8.1.0 partial fire (after BE arm so be_moved is already True
    # and pos.stop = entry when partial returns).
    if partial_profit_at_1r and not pos.partial_taken and pos.shares >= 2:
        if pos.side == "long" and bar_high >= pos.one_r:
            return ExitDecision(reason=EXIT_PARTIAL, price=pos.one_r)
        if pos.side == "short" and bar_low <= pos.one_r:
            return ExitDecision(reason=EXIT_PARTIAL, price=pos.one_r)

    if pos.side == "long":
        # Stop check (pessimistic on simultaneous touch)
        if bar_low <= pos.stop:
            reason = EXIT_BE_STOP if pos.be_moved else EXIT_STOP
            return ExitDecision(reason=reason, price=pos.stop)
        # Target check
        if bar_high >= pos.target:
            return ExitDecision(reason=EXIT_TARGET, price=pos.target)
    else:  # short
        if bar_high >= pos.stop:
            reason = EXIT_BE_STOP if pos.be_moved else EXIT_STOP
            return ExitDecision(reason=reason, price=pos.stop)
        if bar_low <= pos.target:
            return ExitDecision(reason=EXIT_TARGET, price=pos.target)

    # R21 runner_eod_prep -- after the partial fires, capture the runner
    # at a time-based local-max-proxy before the afternoon giveback.
    # 0 = disabled. Fires only after partial_taken=True so losing trades
    # are unaffected. Returns EXIT_RUNNER_EOD_PREP at bar_close.
    if runner_eod_prep_min > 0 and pos.partial_taken and bar_bucket_min >= runner_eod_prep_min:
        return ExitDecision(reason=EXIT_RUNNER_EOD_PREP, price=bar_close)

    # R26 stale_full_exit -- mirror of R21 for the un-partialed cohort.
    # Fires when partial NOT taken AND bar_bucket_min >= stale_full_exit_min.
    # Optional MFE-in-R floor: only close if mfe-in-R is BELOW floor
    # (i.e. trade never came close to 1R). Floor <= 0 disables the
    # gate (always close at cutoff). Catches afternoon driftback on
    # positions that never hit 1R -- replaces legacy sentinel A for
    # Val/Gene under v9.1.128 independence.
    if (
        stale_full_exit_min > 0
        and not pos.partial_taken
        and bar_bucket_min >= stale_full_exit_min
        and pos.risk > 0
    ):
        if pos.side == "long":
            mfe_in_r = (pos.mfe_price - pos.entry_price) / pos.risk
        else:
            mfe_in_r = (pos.entry_price - pos.mfe_price) / pos.risk
        if stale_full_exit_mfe_floor_r <= 0 or mfe_in_r < stale_full_exit_mfe_floor_r:
            return ExitDecision(reason=EXIT_STALE_FULL_EXIT, price=bar_close)

    # EOD flush
    if bar_bucket_min >= eod_cutoff_min:
        return ExitDecision(reason=EXIT_EOD, price=bar_close)

    return None


# ----- v8.1.0: partial-profit accounting helper ----------------------


def apply_partial_fill(pos: OrbPosition, partial_price: float) -> tuple[int, float]:
    """Mutate the position to reflect a half-close at `partial_price`.

    Returns (shares_closed, pnl_dollars_booked). Caller is responsible
    for the broker-side sell and the risk-book partial release.

    On long: pnl = (partial_price - entry_price) * shares_closed
    On short: pnl = (entry_price - partial_price) * shares_closed

    Idempotent: a second call when partial_taken is already True is a
    no-op returning (0, 0.0) -- this defends against double-fire on a
    single bar.
    """
    if pos.partial_taken:
        return (0, 0.0)
    if pos.shares < 2:
        return (0, 0.0)
    half = pos.shares // 2
    if half <= 0:
        return (0, 0.0)
    if pos.side == "long":
        pnl = (partial_price - pos.entry_price) * half
    else:
        pnl = (pos.entry_price - partial_price) * half
    pos.shares -= half
    pos.partial_taken = True
    pos.partial_pnl_dollars += pnl
    return (half, pnl)
