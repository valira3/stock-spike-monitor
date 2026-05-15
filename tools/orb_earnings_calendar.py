"""Earnings calendar for ORB backtest -- auto-refreshed from yfinance.

Coverage: rolling ~20 weeks window for the 12-ticker ORB universe.
timing: 'bmo'=before market open (same day affected),
        'amc'=after market close (next day's ORB fires into post-earnings vol).

Refresh: run scripts/refresh_earnings_calendar.py weekly.
Last refreshed: 2026-05-15 23:31 UTC.
"""

EARNINGS_CALENDAR: dict[str, list[tuple[str, str]]] = {
    "AAPL": [('2026-04-30', 'amc'), ('2026-07-30', 'amc')],
    "MSFT": [('2026-04-29', 'amc'), ('2026-07-29', 'amc')],
    "NVDA": [('2026-05-20', 'amc')],
    "TSLA": [('2026-04-22', 'amc'), ('2026-07-22', 'amc')],
    "META": [('2026-04-29', 'amc'), ('2026-07-29', 'amc')],
    "GOOG": [('2026-04-29', 'amc'), ('2026-07-23', 'amc')],
    "AMZN": [('2026-04-29', 'amc'), ('2026-07-30', 'amc')],
    "AVGO": [('2026-06-03', 'amc')],
    "NFLX": [('2026-07-16', 'amc')],
    "ORCL": [('2026-06-10', 'amc')],
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
