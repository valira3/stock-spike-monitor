"""Earnings calendar for ORB backtest -- manually compiled from public sources.

Coverage: Jan 2025 - Jun 2026 for the 12-ticker ORB universe.
Dates are the actual report date. timing: 'bmo'=before open, 'amc'=after close.
AMC: next day's ORB fires into post-earnings vol. BMO: same day is affected.
Last updated: 2026-05-15. Refresh quarterly before each earnings season.
"""

EARNINGS_CALENDAR: dict[str, list[tuple[str, str]]] = {
    "AAPL": [
        ("2025-01-30", "amc"), ("2025-05-01", "amc"), ("2025-07-31", "amc"),
        ("2025-10-30", "amc"), ("2026-01-29", "amc"), ("2026-05-01", "amc"),
    ],
    "MSFT": [
        ("2025-01-29", "amc"), ("2025-04-30", "amc"), ("2025-07-30", "amc"),
        ("2025-10-29", "amc"), ("2026-01-29", "amc"), ("2026-04-29", "amc"),
    ],
    "NVDA": [
        ("2025-02-26", "amc"), ("2025-05-28", "amc"), ("2025-08-27", "amc"),
        ("2025-11-19", "amc"), ("2026-02-25", "amc"),
    ],
    "TSLA": [
        ("2025-01-29", "amc"), ("2025-04-22", "amc"), ("2025-07-23", "amc"),
        ("2025-10-23", "amc"), ("2026-01-29", "amc"), ("2026-04-22", "amc"),
    ],
    "META": [
        ("2025-01-29", "amc"), ("2025-04-30", "amc"), ("2025-07-30", "amc"),
        ("2025-10-29", "amc"), ("2026-01-29", "amc"), ("2026-04-29", "amc"),
    ],
    "GOOG": [
        ("2025-02-04", "amc"), ("2025-04-29", "amc"), ("2025-07-29", "amc"),
        ("2025-10-29", "amc"), ("2026-02-04", "amc"), ("2026-04-29", "amc"),
    ],
    "AMZN": [
        ("2025-02-06", "amc"), ("2025-05-01", "amc"), ("2025-08-01", "amc"),
        ("2025-10-31", "amc"), ("2026-02-05", "amc"), ("2026-04-30", "amc"),
    ],
    "AVGO": [
        ("2025-03-06", "amc"), ("2025-06-12", "amc"), ("2025-09-04", "amc"),
        ("2025-12-11", "amc"), ("2026-03-05", "amc"),
    ],
    "NFLX": [
        ("2025-01-21", "amc"), ("2025-04-17", "amc"), ("2025-07-17", "amc"),
        ("2025-10-15", "amc"), ("2026-01-21", "amc"), ("2026-04-15", "amc"),
    ],
    "ORCL": [
        ("2025-03-11", "amc"), ("2025-06-10", "amc"), ("2025-09-09", "amc"),
        ("2025-12-09", "amc"), ("2026-03-10", "amc"),
    ],
    "SPY": [],
    "QQQ": [],
}


def is_earnings_window(ticker, date, days_before=1, days_after=0):
    """True if date falls in an earnings blackout window."""
    from datetime import date as _date, timedelta
    sched = EARNINGS_CALENDAR.get(ticker, [])
    if not sched:
        return False
    try:
        d = _date.fromisoformat(date)
    except ValueError:
        return False
    for ed_str, when in sched:
        try:
            ed = _date.fromisoformat(ed_str)
        except ValueError:
            continue
        if when == "amc":
            ed = ed + timedelta(days=1)
        if ed - timedelta(days=days_before) <= d <= ed + timedelta(days=days_after):
            return True
    return False
