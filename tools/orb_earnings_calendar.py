"""Earnings calendar for ORB backtest -- auto-refreshed from yfinance.

Coverage: rolling ~20 weeks window for the 12-ticker ORB universe.
timing: 'bmo'=before market open (same day affected),
        'amc'=after market close (next day's ORB fires into post-earnings vol).

Refresh: run scripts/refresh_earnings_calendar.py weekly.
Last refreshed: 2026-05-18 09:52 UTC.
"""

EARNINGS_CALENDAR: dict[str, list[tuple[str, str]]] = {
    "AAPL": [],
    "MSFT": [],
    "NVDA": [],
    "TSLA": [],
    "META": [],
    "GOOG": [],
    "AMZN": [],
    "AVGO": [],
    "NFLX": [],
    "ORCL": [],
    "SPY": [],
    "QQQ": [],
}


def is_earnings_window(
    ticker: str, date: str, days_before: int = 1, days_after: int = 0
) -> bool:
    """True if date falls in an earnings blackout window.

    AMC reports: window is shifted forward 1 day (the event that matters
    for the next morning's ORB is the overnight report, not the prior session).
    BMO reports: the event day itself is the primary risk day.
    """
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
        if when == 'amc':
            ed = ed + timedelta(days=1)
        if ed - timedelta(days=days_before) <= d <= ed + timedelta(days=days_after):
            return True
    return False
