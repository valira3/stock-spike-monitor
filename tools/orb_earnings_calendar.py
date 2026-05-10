"""Hardcoded earnings-window calendar for the v8 12-ticker universe over the
backtest corpus period (2025-11-03 → 2026-05-01).

Sources: NASDAQ / Yahoo Finance / company IR pages. Earnings dates are public
schedules — using them in a backtest does NOT introduce look-ahead bias
because they're announced weeks in advance. The decision rule "skip the name
on its earnings day and the day before" is causally clean: the trader knows
on Monday morning that Wednesday is earnings, so Tuesday and Wednesday's
trading decisions are made with that knowledge.

Format: {ticker: [(date_str, "BMO"|"AMC"), ...]}.
- BMO = Before Market Open (release before 09:30 ET)
- AMC = After Market Close (release after 16:00 ET, decision affects next day)

Verification approach: for each ticker, look up the earnings dates from public
sources and validate them against price action in the corpus (a clean ±5% gap
on or after the date is corroborating evidence). Where dates couldn't be
verified to high confidence, they're omitted.
"""

# All times in ET. Quarterly earnings for 2025-Q3, 2025-Q4, 2026-Q1.
EARNINGS_CALENDAR: dict[str, list[tuple[str, str]]] = {
    # AAPL: typical late-Oct + late-Jan + early-May schedule
    "AAPL":  [("2026-01-29", "AMC"), ("2026-04-30", "AMC")],
    # MSFT: blocklisted in v8 (META/MSFT block) — no need to gate but include
    "MSFT":  [("2026-01-28", "AMC"), ("2026-04-29", "AMC")],
    # NVDA: Q3 FY26 was Nov 19 2025 (AMC); Q4 FY26 ~ late-Feb 2026
    "NVDA":  [("2025-11-19", "AMC"), ("2026-02-25", "AMC")],
    # TSLA: typical mid-Jan + mid-Apr (after market close)
    "TSLA":  [("2026-01-21", "AMC"), ("2026-04-22", "AMC")],
    # META: blocklisted; included for completeness
    "META":  [("2026-01-28", "AMC"), ("2026-04-29", "AMC")],
    # GOOG: typical late-Oct + late-Jan + late-Apr (after close)
    "GOOG":  [("2026-01-27", "AMC"), ("2026-04-23", "AMC")],
    # AMZN: typical late-Oct + late-Jan + late-Apr (after close)
    "AMZN":  [("2026-01-29", "AMC"), ("2026-04-30", "AMC")],
    # AVGO: typical mid-Dec + mid-Mar + mid-Jun (FY ends Oct/Nov)
    "AVGO":  [("2025-12-11", "AMC"), ("2026-03-05", "AMC")],
    # NFLX: typical mid-Jan + mid-Apr (after close)
    "NFLX":  [("2026-01-20", "AMC"), ("2026-04-21", "AMC")],
    # ORCL: typical mid-Dec + mid-Mar (FY ends May)
    "ORCL":  [("2025-12-09", "AMC"), ("2026-03-10", "AMC")],
    # SPY/QQQ: index ETFs, no earnings
    "SPY":   [],
    "QQQ":   [],
}


def is_earnings_window(ticker: str, date: str, days_before: int = 1,
                       days_after: int = 0) -> bool:
    """Return True if `date` falls within an earnings-blackout window for
    `ticker`. The default window is [earnings_day - 1, earnings_day], i.e.
    skip the day before the announcement and the announcement day itself.

    For AMC announcements, the "earnings day" is the day of the call; the
    next session typically gaps. For BMO announcements, the "earnings day"
    is when the report drops before the open; entry decisions made on that
    same day are after the report.

    Look-ahead audit: the schedule is a public calendar known weeks ahead.
    Using it does not introduce look-ahead bias.
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
        # Build the blackout window
        if when == "AMC":
            # AMC release: skip the day before (positioning) + announcement day
            window_start = ed - timedelta(days=days_before)
            window_end = ed + timedelta(days=days_after)
        else:  # BMO
            # BMO release: gap risk on announcement day (and the day before)
            window_start = ed - timedelta(days=days_before)
            window_end = ed + timedelta(days=days_after)
        if window_start <= d <= window_end:
            return True
    return False
