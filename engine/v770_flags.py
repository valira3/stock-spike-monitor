"""v7.7.0 \u2014 Post-v750 ticker cooldown flag module (Filter #7).

Single source of truth for the v7.7.0 Post-Ditch Cooldown filter.

Motivation: live trading on 2026-05-08 showed that when v750_early_ditch
fires on a ticker, re-entering the SAME ticker on the SAME side within
the next 30 minutes typically compounds the loss. Concrete example:
ORCL_LONG took a $-17.24 v750_early_ditch at 10:21 ET, then ORCL_LONG
re-entered at 11:12 ET and took $-93.25 on a stop_price exit. The v750
fire is a high-confidence signal that this ticker-side is acting wrong
RIGHT NOW; ignoring it on the next entry wastes that signal.

Filter #7 blocks new entries on (ticker, side) for V770_COOLDOWN_MIN
minutes after any v750_early_ditch exit on that same (ticker, side).
Same ticker on the OPPOSITE side is still allowed (intentional: shorts
can still fire when longs got ditched, and vice versa).

Defaults: 30-minute cooldown, ENABLED by default.
"""
from __future__ import annotations
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y", "t")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# Master switch. Default OFF \u2014 promote to ON via Railway env var only.
V770_POST_DITCH_COOLDOWN_ENABLED: bool = _env_bool(
    "V770_POST_DITCH_COOLDOWN_ENABLED", False
)

# Cooldown duration in MINUTES.
V770_POST_DITCH_COOLDOWN_MIN: float = _env_float(
    "V770_POST_DITCH_COOLDOWN_MIN", 30.0
)

SKIP_REASON_V770_COOLDOWN: str = "v770_post_ditch_cooldown"

# Process-local registry: (ticker_upper, side_upper) -> last v750 fire UTC dt.
# Reset on process restart \u2014 acceptable: cooldown semantic is intra-day,
# and a restart is a stronger signal than a 30-min wait.
_FIRES: Dict[Tuple[str, str], datetime] = {}
_LOCK = threading.Lock()


def _normalize_side(side: str) -> str:
    s = (side or "").strip().upper()
    if s in ("LONG", "L", "BUY", "BUY_LONG"):
        return "LONG"
    if s in ("SHORT", "S", "SELL", "SELL_SHORT"):
        return "SHORT"
    return s


def _parse_utc(ts) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        s = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def record_v750_fire(ticker: str, side: str, ts_utc) -> None:
    """Register a v750_early_ditch fire for (ticker, side) at ts_utc.

    Safe to call unconditionally: no-op if inputs are unparseable. Only
    the latest fire per (ticker, side) is retained \u2014 if a later fire
    occurs while still in cooldown the window simply extends.
    """
    if not ticker or not side:
        return
    dt = _parse_utc(ts_utc)
    if dt is None:
        return
    key = (ticker.upper(), _normalize_side(side))
    with _LOCK:
        prior = _FIRES.get(key)
        if prior is None or dt > prior:
            _FIRES[key] = dt


def is_in_cooldown(
    ticker: str, side: str, now_utc=None
) -> Tuple[bool, Optional[float]]:
    """Return (is_blocked, seconds_remaining) for (ticker, side) at now_utc.

    is_blocked is True iff V770_POST_DITCH_COOLDOWN_ENABLED is set, a
    v750 fire was recorded for the same (ticker, side), and the fire
    occurred within the last V770_POST_DITCH_COOLDOWN_MIN minutes.
    """
    if not V770_POST_DITCH_COOLDOWN_ENABLED:
        return (False, None)
    if not ticker or not side:
        return (False, None)
    # v7.8.4: route the now=None fallback through tg._now_utc so the replay
    # clock applies. Defensive -- production callers pass an explicit ts.
    now = _parse_utc(now_utc)
    if now is None:
        try:
            import sys as _sys
            _tg = _sys.modules.get("trade_genius") or _sys.modules.get("__main__")
            now = _tg._now_utc() if _tg is not None else datetime.now(timezone.utc)
        except Exception:
            now = datetime.now(timezone.utc)
    key = (ticker.upper(), _normalize_side(side))
    with _LOCK:
        fire_ts = _FIRES.get(key)
    if fire_ts is None:
        return (False, None)
    window = timedelta(minutes=float(V770_POST_DITCH_COOLDOWN_MIN))
    elapsed = now - fire_ts
    if elapsed >= window:
        return (False, None)
    remaining_sec = (window - elapsed).total_seconds()
    return (True, max(0.0, remaining_sec))


def _reset_for_tests() -> None:
    """Test-only helper: clears the registry."""
    with _LOCK:
        _FIRES.clear()


__all__ = [
    "V770_POST_DITCH_COOLDOWN_ENABLED",
    "V770_POST_DITCH_COOLDOWN_MIN",
    "SKIP_REASON_V770_COOLDOWN",
    "record_v750_fire",
    "is_in_cooldown",
]
