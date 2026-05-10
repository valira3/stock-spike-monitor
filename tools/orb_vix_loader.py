"""VIX daily-close loader for the regime-gate filter.

Source: github.com/datasets/finance-vix (CC-BY-4.0 mirror of CBOE VIX
daily history). Fetched once and cached at data/external/vix-daily.csv.

Look-ahead audit (rule #7b): the gate consumes only `prior_close(D-1)`
to make decisions on day D. The CSV is a public daily series with
~9000 rows from 1990-01-02 onward; using it does not introduce future
information.

Usage:
    from tools.orb_vix_loader import load_vix_closes, vix_close_for
    vix = load_vix_closes("data/external/vix-daily.csv")
    prior = vix_close_for(vix, dates, "2025-11-19")  # returns VIX(D-1)
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path


def load_vix_closes(csv_path: str | Path) -> dict[str, float]:
    """Return {YYYY-MM-DD: vix_close} from the datahub CSV format.

    The CSV columns are DATE, OPEN, HIGH, LOW, CLOSE with dates in
    MM/DD/YYYY format. Empty/malformed rows are silently skipped.
    """
    path = Path(csv_path)
    if not path.is_file():
        return {}
    out: dict[str, float] = {}
    with path.open() as f:
        for row in csv.DictReader(f):
            d = row.get("DATE") or row.get("Date")
            c = row.get("CLOSE") or row.get("Close")
            if not d or not c:
                continue
            try:
                # Try MM/DD/YYYY (datahub format) first, then YYYY-MM-DD
                try:
                    iso = datetime.strptime(d, "%m/%d/%Y").strftime("%Y-%m-%d")
                except ValueError:
                    iso = datetime.strptime(d, "%Y-%m-%d").strftime("%Y-%m-%d")
                out[iso] = float(c)
            except (ValueError, TypeError):
                continue
    return out


def vix_close_for(vix: dict[str, float], dates: list[str],
                  decision_date: str) -> float | None:
    """Return VIX_close on the most recent trading day strictly before
    `decision_date`. Returns None if not available.

    Look-ahead audit: walks backward from `decision_date` to find the
    last calendar day with a VIX print. Never returns the same-day or
    future-day VIX value.
    """
    try:
        dt = datetime.strptime(decision_date, "%Y-%m-%d")
    except ValueError:
        return None
    # Walk back up to 10 days to find last available print (covers
    # weekends + holidays).
    from datetime import timedelta
    for offset in range(1, 11):
        prior = (dt - timedelta(days=offset)).strftime("%Y-%m-%d")
        if prior in vix:
            return vix[prior]
    return None
