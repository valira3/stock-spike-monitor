"""Refresh tools/orb_earnings_calendar.py from yfinance.

Run weekly (or before each earnings season) to keep the blackout calendar
current. Fetches the next ~16 weeks of earnings dates for all 12 ORB
tickers and rewrites the hardcoded Python module.

Usage:
    python scripts/refresh_earnings_calendar.py [--dry-run] [--weeks N]

The script auto-commits and pushes when --dry-run is not set.
"""
import argparse
import datetime
import subprocess
import sys
import os

TICKERS = ["AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOG", "AMZN",
           "AVGO", "NFLX", "ORCL", "SPY", "QQQ"]
ETF_TICKERS = {"SPY", "QQQ"}
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALENDAR_PATH = os.path.join(REPO_ROOT, "tools", "orb_earnings_calendar.py")


def fetch_earnings(ticker: str, weeks_ahead: int = 16) -> list[tuple[str, str]]:
    """Return [(date_iso, timing), ...] for upcoming + recent earnings."""
    try:
        import yfinance as yf
    except ImportError:
        print("  yfinance not installed -- pip install yfinance", file=sys.stderr)
        return []

    if ticker in ETF_TICKERS:
        return []

    try:
        t = yf.Ticker(ticker)
        df = t.earnings_dates
        if df is None or df.empty:
            print(f"  {ticker}: no earnings_dates from yfinance")
            return []

        cutoff_past = datetime.date.today() - datetime.timedelta(weeks=4)
        cutoff_future = datetime.date.today() + datetime.timedelta(weeks=weeks_ahead)

        results = []
        for idx in df.index:
            # idx is a tz-aware datetime
            try:
                d = idx.date() if hasattr(idx, "date") else datetime.date.fromisoformat(str(idx)[:10])
            except Exception:
                continue
            if not (cutoff_past <= d <= cutoff_future):
                continue

            # Determine timing from the hour (16:00 ET = AMC, ~9:00 ET = BMO)
            try:
                hour = idx.hour if hasattr(idx, "hour") else 16
            except Exception:
                hour = 16
            timing = "bmo" if hour < 12 else "amc"
            results.append((str(d), timing))

        results.sort()
        return results

    except Exception as e:
        print(f"  {ticker}: yfinance error: {e}", file=sys.stderr)
        return []


def write_calendar(calendar: dict[str, list[tuple[str, str]]]) -> str:
    """Return the Python source for orb_earnings_calendar.py."""
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f'"""Earnings calendar for ORB backtest -- auto-refreshed from yfinance.',
        f"",
        f"Coverage: rolling ~20 weeks window for the 12-ticker ORB universe.",
        f"timing: 'bmo'=before market open (same day affected),",
        f"        'amc'=after market close (next day's ORB fires into post-earnings vol).",
        f"",
        f"Refresh: run scripts/refresh_earnings_calendar.py weekly.",
        f"Last refreshed: {now}.",
        f'"""',
        f"",
        f"EARNINGS_CALENDAR: dict[str, list[tuple[str, str]]] = {{",
    ]
    for ticker in TICKERS:
        dates = calendar.get(ticker, [])
        lines.append(f'    "{ticker}": {dates!r},')
    lines += [
        "}",
        "",
        "",
        "def is_earnings_window(",
        "    ticker: str, date: str, days_before: int = 1, days_after: int = 0",
        ") -> bool:",
        '    """True if date falls in an earnings blackout window.',
        "",
        "    AMC reports: window is shifted forward 1 day (the event that matters",
        "    for the next morning's ORB is the overnight report, not the prior session).",
        "    BMO reports: the event day itself is the primary risk day.",
        '    """',
        "    from datetime import date as _date, timedelta",
        "",
        "    sched = EARNINGS_CALENDAR.get(ticker, [])",
        "    if not sched:",
        "        return False",
        "    try:",
        "        d = _date.fromisoformat(date)",
        "    except ValueError:",
        "        return False",
        "    for ed_str, when in sched:",
        "        try:",
        "            ed = _date.fromisoformat(ed_str)",
        "        except ValueError:",
        "            continue",
        "        if when == 'amc':",
        "            ed = ed + timedelta(days=1)",
        "        if ed - timedelta(days=days_before) <= d <= ed + timedelta(days=days_after):",
        "            return True",
        "    return False",
        "",
    ]
    return "\n".join(lines)


def git_commit_push(dry_run: bool) -> None:
    if dry_run:
        print("  [dry-run] skipping git commit/push")
        return
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "valira3",
           "GIT_AUTHOR_EMAIL": "valira3@gmail.com",
           "GIT_COMMITTER_NAME": "valira3",
           "GIT_COMMITTER_EMAIL": "valira3@gmail.com"}
    subprocess.run(
        ["git", "add", "tools/orb_earnings_calendar.py"],
        cwd=REPO_ROOT, check=True, env=env
    )
    # Check if there's anything to commit
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=REPO_ROOT, env=env
    )
    if result.returncode == 0:
        print("  No changes to calendar -- already up to date.")
        return
    subprocess.run(
        ["git", "commit", "-m",
         "chore: refresh earnings calendar (auto)\n\nCo-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"],
        cwd=REPO_ROOT, check=True, env=env
    )
    subprocess.run(["git", "push", "origin", "main"], cwd=REPO_ROOT, check=True, env=env)
    print("  Pushed. Railway will redeploy and pick up the new calendar at next bootstrap.")


def main():
    parser = argparse.ArgumentParser(description="Refresh ORB earnings calendar")
    parser.add_argument("--dry-run", action="store_true", help="Print only, don't commit/push")
    parser.add_argument("--weeks", type=int, default=16, help="Weeks ahead to fetch (default 16)")
    args = parser.parse_args()

    print(f"Fetching earnings for {len(TICKERS)} tickers ({args.weeks}w window)...")
    calendar: dict[str, list[tuple[str, str]]] = {}
    total_events = 0
    for ticker in TICKERS:
        dates = fetch_earnings(ticker, weeks_ahead=args.weeks)
        calendar[ticker] = dates
        total_events += len(dates)
        print(f"  {ticker:<6}  {len(dates)} events  {[d[0] for d in dates]}")

    print(f"\nTotal: {total_events} earnings events")

    source = write_calendar(calendar)
    if args.dry_run:
        print("\n--- Generated calendar (dry-run) ---")
        print(source[:800] + "...")
    else:
        with open(CALENDAR_PATH, "w") as f:
            f.write(source)
        print(f"\nWritten to {CALENDAR_PATH}")

    git_commit_push(args.dry_run)


if __name__ == "__main__":
    main()
