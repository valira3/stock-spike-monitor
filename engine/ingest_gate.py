"""engine/ingest_gate.py \u2014 v6.6.0 Trading Gate (Pillar C).

Two-state gate (ALLOW / BLOCK) with hysteresis. Called from
broker/orders.py:execute_breakout() immediately after the
post-loss cooldown check.

Gate modes (SSM_INGEST_GATE_MODE env var):
  off       -> always allow; no audit writes
  dry_run   -> evaluate + log BLOCK decisions; never actually block (default v6.6.0)
  enforce   -> evaluate + block on RED tickers (v6.6.1+, requires sign-off)

Hysteresis (Decision A4):
  Enter BLOCKED only after continuous RED for SLA_GATE_RED_MIN seconds (default 300 s).
  Exit BLOCKED only after continuous GREEN for SLA_GATE_GREEN_MIN seconds (default 120 s).

Thread safety: _gate_state protected by _gate_lock. SLA state reads
go through ingest.sla._sla_lock. Gate check is synchronous and in-memory only.

Decision P2 (locked): two-state only. No Yellow action. No half-size cap.
Decision P1 (locked): REST_ONLY ceiling is 20 min (GATE_REST_ONLY_CEILING_S).
Decision A5 (locked): enforce flip requires Val + Devi + Reese sign-off.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import ingest_config as _cfg

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures (Section C.1 of architecture doc)
# ---------------------------------------------------------------------------

@dataclass
class GateDecision:
    """A single gate evaluation result. Written to ingest_gate_decisions table."""
    ticker: str
    decision_ts: str       # ISO 8601 UTC
    decision: str          # "allow" | "block"
    reason: str            # human-readable
    gate_mode: str         # "dry_run" | "enforce" | "off"
    overridden: bool       # True when mode != "enforce"
    override_reason: str   # e.g. "SSM_INGEST_GATE_MODE=dry_run"


@dataclass
class _TickerGateState:
    ticker: str
    current_color: str           # last known SLA color for this ticker
    red_since: Optional[float]   # monotonic time when color first went RED (None if not RED)
    green_since: Optional[float] # monotonic time when color first went GREEN (None if not GREEN)
    blocked: bool                # current gate position


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_gate_state: dict = {}   # str -> _TickerGateState
_gate_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Gate mode resolution
# ---------------------------------------------------------------------------

def _resolve_gate_mode() -> str:
    """Return the effective gate mode from env vars.

    SSM_INGEST_GATE_DISABLED=1 is the legacy break-glass override (equiv to off).
    SSM_INGEST_GATE_MODE = off | dry_run | enforce (default: dry_run)
    """
    if os.environ.get("SSM_INGEST_GATE_DISABLED") == "1":
        return "off"
    mode = os.environ.get("SSM_INGEST_GATE_MODE", "dry_run").lower().strip()
    if mode not in ("off", "dry_run", "enforce"):
        logger.warning("[INGEST-GATE] Unknown gate mode %r; defaulting to dry_run", mode)
        return "dry_run"
    return mode


# ---------------------------------------------------------------------------
# Hysteresis state machine
# ---------------------------------------------------------------------------

def _update_ticker_gate_state(gs: _TickerGateState, effective_color: str) -> None:
    """Apply hysteresis rules to update the gate state for one ticker.

    ALLOWED -> BLOCKED: color has been RED continuously for SLA_GATE_RED_MIN s.
    BLOCKED -> ALLOWED: color has been GREEN continuously for SLA_GATE_GREEN_MIN s.
    """
    now = time.monotonic()

    if effective_color != gs.current_color:
        gs.current_color = effective_color
        # Reset timers on color change
        gs.red_since = now if effective_color == "red" else None
        gs.green_since = now if effective_color == "green" else None
    else:
        # Same color: initialize timer if not set
        if effective_color == "red" and gs.red_since is None:
            gs.red_since = now
        if effective_color == "green" and gs.green_since is None:
            gs.green_since = now

    if not gs.blocked:
        # Check if we should enter BLOCKED
        if (
            effective_color == "red"
            and gs.red_since is not None
            and (now - gs.red_since) >= _cfg.SLA_GATE_RED_MIN
        ):
            gs.blocked = True
            logger.warning(
                "[INGEST-GATE] %s transitioning to BLOCKED (RED for %.1f s >= %.0f s threshold)",
                gs.ticker,
                now - gs.red_since,
                _cfg.SLA_GATE_RED_MIN,
            )
    else:
        # Check if we should exit BLOCKED (return to ALLOWED)
        if (
            effective_color == "green"
            and gs.green_since is not None
            and (now - gs.green_since) >= _cfg.SLA_GATE_GREEN_MIN
        ):
            gs.blocked = False
            logger.info(
                "[INGEST-GATE] %s transitioning to ALLOWED (GREEN for %.1f s >= %.0f s threshold)",
                gs.ticker,
                now - gs.green_since,
                _cfg.SLA_GATE_GREEN_MIN,
            )


def _build_reason(gs: _TickerGateState, effective_color: str) -> str:
    """Build a human-readable reason string for the gate decision."""
    now = time.monotonic()
    if gs.blocked:
        red_s = (now - gs.red_since) if gs.red_since else 0.0
        return (
            f"ticker {gs.ticker} RED for {red_s:.0f}s "
            f"(threshold={_cfg.SLA_GATE_RED_MIN:.0f}s)"
        )
    return f"ticker {gs.ticker} color={effective_color}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Async audit write (non-blocking)
# ---------------------------------------------------------------------------

def _write_audit(decision: GateDecision) -> None:
    """Write gate decision to audit DB. Runs synchronously but is fail-safe."""
    try:
        from ingest.audit import AuditLog
        AuditLog.record_gate_decision(
            ticker=decision.ticker,
            decision=decision.decision,
            reason=decision.reason,
            gate_mode=decision.gate_mode,
            overridden=decision.overridden,
            override_reason=decision.override_reason,
        )
    except Exception as e:
        logger.debug("[INGEST-GATE] audit write failed: %s", e)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def evaluate_gate(ticker: str) -> GateDecision:
    """Evaluate the ingest gate for a ticker. Called from execute_breakout().

    Thread-safe. Never raises \u2014 exceptions fall back to allow.
    Synchronous: reads in-memory SLA state only; no I/O in the hot path.

    Returns a GateDecision with decision="allow" or "block".
    In dry_run mode: always returns "allow" but writes audit record for "block" decisions.
    In enforce mode: returns "block" which causes execute_breakout() to return early.
    """
    mode = _resolve_gate_mode()

    if mode == "off":
        return GateDecision(
            ticker=ticker,
            decision_ts=_utc_now_iso(),
            decision="allow",
            reason="gate_mode=off",
            gate_mode="off",
            overridden=True,
            override_reason="SSM_INGEST_GATE_MODE=off",
        )

    # Resolve effective SLA color (worst of ticker + global)
    try:
        from ingest.sla import get_health_state, _worst_color, _is_rth
        ticker_state = get_health_state(ticker)
        global_state = get_health_state(None)
        effective_color = _worst_color(ticker_state.color, global_state.color)
        # Decision P3: outside RTH, always allow (thresholds not enforced for gating)
        if not _is_rth():
            effective_color = "green"
    except Exception as e:
        logger.debug("[INGEST-GATE] SLA state unavailable for %s: %s; failing open", ticker, e)
        effective_color = "green"  # fail-open: ingest state unavailable -> allow

    with _gate_lock:
        gs = _gate_state.setdefault(
            ticker,
            _TickerGateState(
                ticker=ticker,
                current_color="green",
                red_since=None,
                green_since=None,
                blocked=False,
            ),
        )
        _update_ticker_gate_state(gs, effective_color)
        raw_decision = "block" if gs.blocked else "allow"
        reason = _build_reason(gs, effective_color)

    # In dry_run mode: log block decisions but never actually block
    overridden = mode != "enforce"
    override_reason = f"SSM_INGEST_GATE_MODE={mode}" if overridden else ""

    # For dry_run: use the raw_decision for audit, but return allow
    effective_decision = raw_decision if mode == "enforce" else "allow"

    decision_obj = GateDecision(
        ticker=ticker,
        decision_ts=_utc_now_iso(),
        decision=effective_decision,
        reason=reason,
        gate_mode=mode,
        overridden=overridden,
        override_reason=override_reason,
    )

    # Write audit for block events (or all events in enforce mode)
    if mode != "off" and (raw_decision == "block" or mode == "enforce"):
        # Write audit record with the raw (un-overridden) decision for dry_run
        audit_decision = GateDecision(
            ticker=ticker,
            decision_ts=decision_obj.decision_ts,
            decision=raw_decision,
            reason=reason,
            gate_mode=mode,
            overridden=overridden,
            override_reason=override_reason,
        )
        _write_audit(audit_decision)

    if raw_decision == "block":
        logger.warning(
            "[INGEST-GATE] %s %s for %s: %s (mode=%s)",
            "DRY_RUN" if mode == "dry_run" else "ENFORCED",
            raw_decision.upper(),
            ticker,
            reason,
            mode,
        )

    return decision_obj


def get_gate_state_summary() -> dict:
    """Return current gate state for all tickers. Used by /api/state."""
    with _gate_lock:
        return {
            ticker: {
                "blocked": gs.blocked,
                "current_color": gs.current_color,
            }
            for ticker, gs in _gate_state.items()
        }
