"""v6.16.1 \u2014 earnings_watcher.state: position persistence layer.

Manages /data/earnings_watcher/open_positions.json (falls back to
/tmp/earnings_watcher/ if /data is not writable).

Schema:
  {
    ticker: {
      entry_px: float,
      entry_ts_utc: str,       # ISO 8601
      qty: int,
      side: str,               # 'long' | 'short'
      notional: float,
      conv: float,
      peak_pct: float,
      trough_pct: float,
      trail_active: bool,
      trail_stop: float,
      order_id: str,
      last_update_ts: str,     # ISO 8601
    }
  }

Hard boundary: MUST NOT import from eye_of_tiger or trade_genius.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("earnings_watcher")

# ---------------------------------------------------------------------------
# Path resolution (tolerates /data missing)
# ---------------------------------------------------------------------------

_PATH_CANDIDATES = [
    "/data/earnings_watcher/open_positions.json",
    "/tmp/earnings_watcher/open_positions.json",
]


def _positions_path() -> Path:
    for candidate in _PATH_CANDIDATES:
        p = Path(candidate)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            # Probe writability by touching a side-car file
            probe = p.parent / ".write_probe"
            probe.write_text("ok")
            probe.unlink()
            return p
        except OSError:
            continue
    # Last resort: use /tmp directly
    fallback = Path("/tmp/open_positions_ew.json")
    logger.warning("[EW-STATE] all preferred paths unwritable, falling back to %s", fallback)
    return fallback


_ACTIVE_PATH: Path | None = None


def _path() -> Path:
    global _ACTIVE_PATH
    if _ACTIVE_PATH is None:
        _ACTIVE_PATH = _positions_path()
        logger.info("[EW-STATE] positions file: %s", _ACTIVE_PATH)
    return _ACTIVE_PATH


# ---------------------------------------------------------------------------
# Core load / save
# ---------------------------------------------------------------------------

def load_open_positions() -> Dict[str, Dict[str, Any]]:
    """Load and return the open positions dict from disk.

    Returns empty dict on any error (missing file, corrupt JSON).
    """
    p = _path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        if not isinstance(data, dict):
            logger.warning("[EW-STATE] positions file malformed, resetting")
            return {}
        return data
    except Exception as exc:
        logger.warning("[EW-STATE] load_open_positions error: %s", exc)
        return {}


def save_open_positions(d: Dict[str, Dict[str, Any]]) -> None:
    """Atomic write of positions dict to disk (write .tmp then rename)."""
    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(p) + ".tmp"
        Path(tmp).write_text(json.dumps(d, indent=2))
        os.replace(tmp, str(p))
        logger.debug("[EW-STATE] saved %d positions", len(d))
    except Exception as exc:
        logger.warning("[EW-STATE] save_open_positions error: %s", exc)


# ---------------------------------------------------------------------------
# Mutation helpers
# ---------------------------------------------------------------------------

def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_position(ticker: str, **kwargs: Any) -> None:
    """Add or overwrite a position record for ticker."""
    positions = load_open_positions()
    positions[ticker] = {
        "entry_px": 0.0,
        "entry_ts_utc": _now_utc_iso(),
        "qty": 0,
        "side": "long",
        "notional": 0.0,
        "conv": 0.0,
        "peak_pct": 0.0,
        "trough_pct": 0.0,
        "trail_active": False,
        "trail_stop": 0.0,
        "order_id": "",
        "last_update_ts": _now_utc_iso(),
        **kwargs,
    }
    logger.info("[EW-STATE] add_position ticker=%s", ticker)
    save_open_positions(positions)


def remove_position(ticker: str) -> None:
    """Remove a position by ticker. No-op if not present."""
    positions = load_open_positions()
    if ticker in positions:
        del positions[ticker]
        logger.info("[EW-STATE] remove_position ticker=%s", ticker)
        save_open_positions(positions)
    else:
        logger.debug("[EW-STATE] remove_position ticker=%s not found (no-op)", ticker)


def update_position(ticker: str, **kwargs: Any) -> None:
    """Partial update of an existing position. No-op if ticker not found."""
    positions = load_open_positions()
    if ticker not in positions:
        logger.warning("[EW-STATE] update_position ticker=%s not found", ticker)
        return
    positions[ticker].update(kwargs)
    positions[ticker]["last_update_ts"] = _now_utc_iso()
    save_open_positions(positions)


# ---------------------------------------------------------------------------
# evaluated_today \u2014 per-window per-strategy idempotency cache (v7.2.0)
# ---------------------------------------------------------------------------
# v7.2.0 schema: {date_iso: {window: {strategy: [ticker, ...]}}}
# Legacy schema: {date_iso: {window: [ticker, ...]}}  (auto-migrated under "dmi")
# Stored in /data/earnings_watcher/evaluated_today.json (same fallback logic).

_EVAL_PATH_CANDIDATES = [
    "/data/earnings_watcher/evaluated_today.json",
    "/tmp/earnings_watcher/evaluated_today.json",
]


def _eval_path() -> Path:
    for candidate in _EVAL_PATH_CANDIDATES:
        p = Path(candidate)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            probe = p.parent / ".write_probe_eval"
            probe.write_text("ok")
            probe.unlink()
            return p
        except OSError:
            continue
    fallback = Path("/tmp/evaluated_today_ew.json")
    logger.warning("[EW-STATE] eval path unwritable, falling back to %s", fallback)
    return fallback


def _migrate_legacy_schema(data: dict) -> dict:
    """Migrate {date: {window: [tickers]}} -> {date: {window: {strategy: [tickers]}}}.

    Legacy entries (pre-v7.2.0) collapse into the "dmi" strategy bucket.
    Idempotent: returns data unchanged if already in v7.2.0 shape.
    """
    if not isinstance(data, dict):
        return {}
    changed = False
    for date_iso, windows in list(data.items()):
        if not isinstance(windows, dict):
            data[date_iso] = {}
            changed = True
            continue
        for window, payload in list(windows.items()):
            if isinstance(payload, list):
                # Legacy list → wrap as {"dmi": [...]}
                data[date_iso][window] = {"dmi": list(payload)}
                changed = True
            elif not isinstance(payload, dict):
                data[date_iso][window] = {}
                changed = True
    if changed:
        logger.info("[EW-STATE] migrated evaluated_today legacy schema -> v7.2.0")
    return data


def _load_evaluated_today() -> dict:
    """Load evaluated_today dict from disk. Returns {} on error.

    Auto-migrates pre-v7.2.0 (list-valued) entries to per-strategy dicts.
    """
    p = _eval_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        if not isinstance(data, dict):
            return {}
        return _migrate_legacy_schema(data)
    except Exception as exc:
        logger.warning("[EW-STATE] load_evaluated_today error: %s", exc)
        return {}


def _save_evaluated_today(d: dict) -> None:
    """Atomic write of evaluated_today dict to disk."""
    p = _eval_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(p) + ".tmp"
        Path(tmp).write_text(json.dumps(d, indent=2))
        os.replace(tmp, str(p))
    except Exception as exc:
        logger.warning("[EW-STATE] save_evaluated_today error: %s", exc)


def get_evaluated_tickers(date_iso: str, window: str, strategy: str = "dmi") -> list:
    """Return tickers already evaluated in this date/window/strategy bucket."""
    data = _load_evaluated_today()
    bucket = data.get(date_iso, {}).get(window, {})
    if isinstance(bucket, dict):
        return list(bucket.get(strategy, []))
    # Defensive: legacy list survived migration somehow \u2014 treat as dmi.
    if isinstance(bucket, list) and strategy == "dmi":
        return list(bucket)
    return []


def mark_ticker_evaluated(
    date_iso: str, window: str, ticker: str, strategy: str = "dmi"
) -> None:
    """Add ticker to the evaluated set for date/window/strategy. Idempotent."""
    data = _load_evaluated_today()
    data.setdefault(date_iso, {}).setdefault(window, {}).setdefault(strategy, [])
    bucket = data[date_iso][window][strategy]
    if ticker not in bucket:
        bucket.append(ticker)
    _save_evaluated_today(data)


def clear_window_evaluated(
    date_iso: str, window: str, strategy: str | None = None
) -> None:
    """Clear evaluated list at window start.

    If strategy is None, clears every strategy bucket for the window
    (preserves the legacy single-call behavior that the runner uses to
    reset all strategies at window start).
    """
    data = _load_evaluated_today()
    windows = data.get(date_iso, {})
    if window in windows:
        if strategy is None:
            windows[window] = {}
        else:
            bucket = windows[window]
            if isinstance(bucket, dict):
                bucket[strategy] = []
            else:
                windows[window] = {}
    _save_evaluated_today(data)
    logger.info(
        "[EW-STATE] cleared evaluated_today date=%s window=%s strategy=%s",
        date_iso, window, strategy or "*",
    )
