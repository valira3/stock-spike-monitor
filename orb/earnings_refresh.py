"""v10.0.1 -- in-process earnings calendar refresh.

Replaces the (now-retired) GHA cron that used to keep
`tools/orb_earnings_calendar.py` populated. The bot's scheduler calls
`fire_refresh()` on a weekly cadence; this module:

  1. Fetches earnings dates from Yahoo Finance via the existing
     `tools/orb_earnings_fetcher.py:fetch_earnings_dates` helper.
  2. Writes the new dict to `tools/orb_earnings_calendar.py` via the
     existing `write_calendar` helper (atomic file replace).
  3. `importlib.reload`s the calendar module so the already-bound
     `is_earnings_window` function reference -- held by every
     OrbEngine instance via _resolve_earnings_fn() -- starts returning
     the fresh data on the next call. (Python name lookup in the
     function body is dynamic against the module __dict__, so a
     reload updates EARNINGS_CALENDAR in-place for callers.)
  4. Updates a thread-safe module-level "state" so `live_runtime.snapshot()`
     can surface "last refresh / N events" to the dashboard for
     staleness alerting.

The lxml-missing trap is a known failure mode (see
[[earnings-calendar-refresh-lxml-gotcha]]): yfinance.Ticker.earnings_dates
silently returns nothing if lxml isn't installed -- the GHA workflow
that previously shipped the empty calendar carried this exact bug for
months. This module detects "zero events across all tickers" and
treats it as a FAIL (state.last_status = "empty_payload") rather than
overwriting the file with empty data. Operator sees the failure in
the UI immediately.
"""
from __future__ import annotations

import importlib
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)


# Universe to fetch. Mirrors tools/orb_earnings_fetcher.py:UNIVERSE.
EARNINGS_UNIVERSE: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOG",
    "AMZN", "AVGO", "NFLX", "ORCL", "SPY", "QQQ",
)

# Default refresh window: 90 days back, 30 days forward. Backtest needs a
# wider lookback so the fetcher CLI tool overrides this; for live refresh
# we only care about the next ~30 days of upcoming earnings.
DEFAULT_LOOKBACK_DAYS = 90
DEFAULT_LOOKAHEAD_DAYS = 30

# Where the .py module lives (writable -- the fetcher overwrites it).
DEFAULT_OUT_PATH = (
    Path(__file__).resolve().parent.parent / "tools" / "orb_earnings_calendar.py"
)


@dataclass(frozen=True)
class RefreshState:
    last_run_iso: str          # UTC ISO timestamp of the most recent attempt
    last_status: str           # "ok" | "empty_payload" | "fetch_failed" | "reload_failed"
    n_events: int              # total events across all tickers in the new calendar
    n_tickers_with_events: int # how many tickers have at least 1 event
    error_msg: str             # short error string when status != "ok"


_lock = threading.Lock()
_state: Optional[RefreshState] = None


def get_state() -> Optional[RefreshState]:
    with _lock:
        return _state


def _set_state(s: RefreshState) -> None:
    global _state
    with _lock:
        _state = s


def to_snapshot_dict() -> dict:
    """Serialize for /api/state. Always returns a well-shaped dict."""
    s = get_state()
    if s is None:
        return {
            "last_run_iso": "",
            "last_status": "never_run",
            "n_events": 0,
            "n_tickers_with_events": 0,
            "error_msg": "",
        }
    return {
        "last_run_iso": s.last_run_iso,
        "last_status": s.last_status,
        "n_events": s.n_events,
        "n_tickers_with_events": s.n_tickers_with_events,
        "error_msg": s.error_msg,
    }


def fire_refresh(
    *,
    universe: tuple[str, ...] = EARNINGS_UNIVERSE,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    lookahead_days: int = DEFAULT_LOOKAHEAD_DAYS,
    out_path: Path = DEFAULT_OUT_PATH,
) -> RefreshState:
    """Pull fresh earnings dates from Yahoo Finance, overwrite the
    calendar module, reload it, and update state. Thread-safe via the
    underlying _lock in _set_state; idempotent under repeated calls.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    logger.info("[V100-EARNINGS-REFRESH] starting (universe=%d tickers)", len(universe))

    # Fetch
    try:
        from tools.orb_earnings_fetcher import fetch_earnings_dates, write_calendar
        from datetime import datetime as _dt, timedelta as _td

        today = _dt.utcnow()
        start = (today - _td(days=lookback_days)).strftime("%Y-%m-%d")
        end = (today + _td(days=lookahead_days)).strftime("%Y-%m-%d")

        dates: dict[str, list[tuple[str, str]]] = {}
        for tk in universe:
            try:
                events = fetch_earnings_dates(tk, start, end)
            except Exception as e:
                logger.warning("[V100-EARNINGS-REFRESH] %s fetch failed: %s", tk, e)
                events = []
            dates[tk] = events
    except Exception as e:
        msg = f"fetch_earnings_dates import/call failed: {e}"
        logger.exception("[V100-EARNINGS-REFRESH] %s", msg)
        s = RefreshState(
            last_run_iso=now_iso, last_status="fetch_failed",
            n_events=0, n_tickers_with_events=0, error_msg=str(e)[:200],
        )
        _set_state(s)
        return s

    # Sanity: if every ticker returned 0 events, that's the lxml-missing
    # trap (or Yahoo is fully down). Don't overwrite the existing calendar
    # with an empty payload -- log + surface the failure.
    total_events = sum(len(v) for v in dates.values())
    tickers_with_events = sum(1 for v in dates.values() if v)
    if total_events == 0:
        msg = "all tickers returned 0 events (lxml missing? yfinance down?)"
        logger.warning("[V100-EARNINGS-REFRESH] %s -- NOT overwriting calendar", msg)
        s = RefreshState(
            last_run_iso=now_iso, last_status="empty_payload",
            n_events=0, n_tickers_with_events=0, error_msg=msg,
        )
        _set_state(s)
        return s

    # Write
    try:
        write_calendar(dates, out_path)
    except Exception as e:
        msg = f"write_calendar failed: {e}"
        logger.exception("[V100-EARNINGS-REFRESH] %s", msg)
        s = RefreshState(
            last_run_iso=now_iso, last_status="write_failed",
            n_events=total_events, n_tickers_with_events=tickers_with_events,
            error_msg=str(e)[:200],
        )
        _set_state(s)
        return s

    # Reload so the already-resolved is_earnings_window function picks up
    # the new dict. Function-body name lookup is dynamic against the
    # module __dict__, so an in-place reload IS visible to existing
    # bindings on the next call.
    try:
        import tools.orb_earnings_calendar as _cal_mod
        importlib.reload(_cal_mod)
    except Exception as e:
        msg = f"importlib.reload failed: {e}"
        logger.exception("[V100-EARNINGS-REFRESH] %s -- new file on disk but live process still on old data", msg)
        s = RefreshState(
            last_run_iso=now_iso, last_status="reload_failed",
            n_events=total_events, n_tickers_with_events=tickers_with_events,
            error_msg=str(e)[:200],
        )
        _set_state(s)
        return s

    s = RefreshState(
        last_run_iso=now_iso, last_status="ok",
        n_events=total_events, n_tickers_with_events=tickers_with_events,
        error_msg="",
    )
    _set_state(s)
    logger.info(
        "[V100-EARNINGS-REFRESH] ok n_events=%d tickers_with_events=%d/%d",
        total_events, tickers_with_events, len(universe),
    )
    return s
