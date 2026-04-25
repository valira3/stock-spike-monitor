"""state_snapshot / state_diff — JSON-friendly state captures for goldens.

Captures the subset of trade_genius globals that scenarios mutate.
state_diff returns the recursively-computed diff of two snapshots so
goldens record only the deltas.
"""
from __future__ import annotations

import copy

# Globals captured for every action's state_delta.
CAPTURE_KEYS = (
    "positions",
    "short_positions",
    "daily_entry_count",
    "daily_short_entry_count",
    "daily_entry_date",
    "daily_short_entry_date",
    "paper_cash",
    "_trading_halted",
    "_trading_halted_reason",
    "trade_history",
    "short_trade_history",
    "paper_trades",
    "paper_all_trades",
    "or_high",
    "or_low",
    "pdc",
    "_scan_paused",
    "_last_exit_time",
)


def _to_jsonable(val):
    """Convert datetime-like and other non-JSON values to strings.

    state_diff snapshots include dicts with datetime values
    (_last_exit_time). Render them to ISO strings so JSON round-trip
    is faithful.
    """
    if isinstance(val, dict):
        return {k: _to_jsonable(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_to_jsonable(v) for v in val]
    if isinstance(val, tuple):
        return [_to_jsonable(v) for v in val]
    # datetime
    if hasattr(val, "isoformat") and not isinstance(val, str):
        try:
            return {"__dt__": val.isoformat()}
        except Exception:
            return str(val)
    return val


def state_snapshot(module, keys=CAPTURE_KEYS) -> dict:
    """Deepcopy + JSON-render a fixed set of module attributes."""
    out = {}
    for k in keys:
        v = getattr(module, k, None)
        out[k] = _to_jsonable(copy.deepcopy(v))
    return out


def state_diff(before: dict, after: dict) -> dict:
    """Recursive diff: returns mapping of changed keys -> {before, after}.

    For dict values: descends one level so nested mutations are
    surfaced individually (positions["AAPL"]["stop"] = 99 etc.).
    For list values: stores the entire after-list when it differs.
    """
    out = {}
    keys = set(before) | set(after)
    for k in keys:
        b = before.get(k, _MISSING)
        a = after.get(k, _MISSING)
        if b == a:
            continue
        if isinstance(b, dict) and isinstance(a, dict):
            sub = _dict_diff(b, a)
            if sub:
                out[k] = sub
        else:
            out[k] = {"before": b, "after": a}
    return out


_MISSING = object()


def _dict_diff(b: dict, a: dict) -> dict:
    out = {}
    keys = set(b) | set(a)
    for k in keys:
        bv = b.get(k, _MISSING)
        av = a.get(k, _MISSING)
        if bv == av:
            continue
        if bv is _MISSING:
            out[k] = {"added": av}
        elif av is _MISSING:
            out[k] = {"removed": bv}
        else:
            if isinstance(bv, dict) and isinstance(av, dict):
                sub = _dict_diff(bv, av)
                if sub:
                    out[k] = sub
            else:
                out[k] = {"before": bv, "after": av}
    return out
