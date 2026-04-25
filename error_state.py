"""error_state.py \u2014 per-executor error ring + dedup gate (v4.11.0).

Owns the today-only error counts that drive the dashboard health pill
and decides when an error event should fan out to the executor's
Telegram channel. Decoupled from telegram_commands.py: callers (the
report_error wrapper in trade_genius.py) are responsible for the
actual send call. This keeps the module trivially testable.

Severity tiers:
  warning  \u2014 trading-relevant warning (amber pill if no red events)
  error    \u2014 ERROR-level event (red pill)
  critical \u2014 CRITICAL-level event (red pill)

Pill color rule (computed in snapshot()):
  green   if count == 0
  warning if count > 0 AND no error/critical events today
  red     if any error/critical event today
"""
from __future__ import annotations

from collections import deque
from threading import Lock
from time import time as _time

# Per-executor error rings. Keys: "main", "val", "gene". Bounded so a
# runaway error storm cannot grow memory unbounded; the dashboard only
# shows the last 10 entries anyway.
_RING_MAXLEN = 50

_ERROR_RINGS: dict[str, deque] = {
    "main": deque(maxlen=_RING_MAXLEN),
    "val":  deque(maxlen=_RING_MAXLEN),
    "gene": deque(maxlen=_RING_MAXLEN),
}

# Per-(executor, code) timestamp of the last Telegram dispatch. Used by
# the dedup gate so a flapping error code does not spam the channel.
_DEDUP: dict[tuple[str, str], float] = {}
_DEDUP_COOLDOWN_S = 300  # 5 minutes

_LOCK = Lock()

SEV_WARNING = "warning"
SEV_ERROR = "error"
SEV_CRITICAL = "critical"

_VALID_SEVERITIES = (SEV_WARNING, SEV_ERROR, SEV_CRITICAL)
_VALID_EXECUTORS = ("main", "val", "gene")


def _normalize_executor(executor: str) -> str:
    e = (executor or "").strip().lower()
    if e not in _VALID_EXECUTORS:
        # Default unknown executors to main rather than dropping silently;
        # caller bug in the executor name should still surface a count.
        return "main"
    return e


def _normalize_severity(severity: str) -> str:
    s = (severity or "").strip().lower()
    if s not in _VALID_SEVERITIES:
        return SEV_ERROR
    return s


def record_error(
    executor: str,
    code: str,
    severity: str,
    summary: str,
    detail: str = "",
    *,
    ts: str | None = None,
    now_fn=_time,
) -> bool:
    """Append an error event and return True iff Telegram should fire now.

    Args:
      executor: "main" | "val" | "gene"
      code:     short uppercase identifier (e.g. "ORDER_REJECT")
      severity: "warning" | "error" | "critical"
      summary:  one-line description shown in the dashboard dropdown
      detail:   longer text included in the Telegram message
      ts:       ISO timestamp string for the dashboard entry (optional;
                callers pass _now_et().isoformat() so the harness clock
                is honored)
      now_fn:   wall-clock provider used ONLY for the dedup decision;
                injectable so tests can advance it without sleeping.

    Returns True iff the (executor, code) cooldown has elapsed and the
    caller should send to Telegram now.
    """
    ex = _normalize_executor(executor)
    sev = _normalize_severity(severity)
    code_s = (code or "UNKNOWN").strip().upper().replace(" ", "_")
    summ = (summary or "").strip()
    det = (detail or "").strip()
    ts_s = ts or ""

    entry = {
        "ts": ts_s,
        "code": code_s,
        "severity": sev,
        "summary": summ,
        "detail": det,
    }

    with _LOCK:
        _ERROR_RINGS[ex].append(entry)
        key = (ex, code_s)
        last = _DEDUP.get(key, 0.0)
        now = float(now_fn())
        if now - last >= _DEDUP_COOLDOWN_S:
            _DEDUP[key] = now
            return True
        return False


def _severity_tier(entries) -> str:
    """Compute the pill color from the entries list.

    green   if no entries
    warning if only warning-tier entries
    red     if any error/critical
    """
    if not entries:
        return "green"
    for e in entries:
        if e.get("severity") in (SEV_ERROR, SEV_CRITICAL):
            return "red"
    return "warning"


def snapshot(executor: str) -> dict:
    """Return today's snapshot for the dashboard.

    Shape:
      {
        "executor": str,
        "count":    int,        # total events today
        "severity": "green" | "warning" | "red",
        "entries":  [last 10, newest first, each as the record dict],
      }
    """
    ex = _normalize_executor(executor)
    with _LOCK:
        ring = _ERROR_RINGS[ex]
        entries = list(ring)
    # Newest-first for the dashboard dropdown.
    entries_rev = list(reversed(entries))
    return {
        "executor": ex,
        "count": len(entries),
        "severity": _severity_tier(entries),
        "entries": entries_rev[:10],
    }


def reset_daily(executor: str | None = None) -> None:
    """Clear today's error counts.

    Args:
      executor: if None, reset all three; else reset just that executor.
    Also clears the dedup table so a code that fired yesterday does not
    suppress today's first occurrence.
    """
    with _LOCK:
        if executor is None:
            for k in _ERROR_RINGS:
                _ERROR_RINGS[k].clear()
            _DEDUP.clear()
        else:
            ex = _normalize_executor(executor)
            _ERROR_RINGS[ex].clear()
            # Drop dedup keys belonging to this executor only.
            for k in [k for k in _DEDUP if k[0] == ex]:
                _DEDUP.pop(k, None)


def _reset_for_tests() -> None:
    """Test-only: wipe ALL state. Not for production use."""
    with _LOCK:
        for k in _ERROR_RINGS:
            _ERROR_RINGS[k].clear()
        _DEDUP.clear()
