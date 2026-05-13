"""Prior-day SPY return loader for the v9.0.0 regime-skip gate.

The gate (`orb.day_gates.evaluate_day` with
`skip_prior_spy_ret_lt_bps` set) consumes the prior session's
SPY close-to-close return in bps to decide whether to block the
day. R12 research showed the strategy bleeds ~$208/day on days
where prior SPY return was in the (-1.0%, -0.5%) band; the gate
filters those out.

Two data sources, tried in order:
  1. `/data/bars/<YYYY-MM-DD>/SPY.jsonl` (production bar archive
     written by bar_archive.py). Used in live.
  2. `data/external/spy-daily.csv` (datahub-style CSV). Fallback
     for backtests + local dev when the bar archive is missing.

Fail behavior: returns None when neither source is available.
Callers (engine.start_new_session via day_gates.evaluate_day)
should treat None as "fail-open" unless
`fail_closed_on_missing_spy=True` is set in DayGateConfig.

Look-ahead audit (rule #7b): both sources expose only closes at
or before the decision date. The function explicitly walks
BACKWARD from `decision_date`; same-day or future data is never
consulted.
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


DEFAULT_BAR_ARCHIVE_ROOT = "/data/bars"
DEFAULT_CSV_PATH = "data/external/spy-daily.csv"


def _last_rth_close_from_jsonl(jsonl_path: Path) -> Optional[float]:
    """Scan a SPY.jsonl bar file, return the last RTH bar's close
    (et_bucket in [0930, 1559]). Returns None on missing/empty file.
    """
    if not jsonl_path.is_file():
        return None
    last_close: Optional[float] = None
    try:
        with jsonl_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    bar = json.loads(line)
                except json.JSONDecodeError:
                    continue
                bucket = bar.get("et_bucket")
                # Accept string ("0930") or int (570 minute-of-day).
                if isinstance(bucket, str):
                    try:
                        b_int = int(bucket)
                    except ValueError:
                        continue
                    if not (930 <= b_int <= 1559):
                        continue
                elif isinstance(bucket, int):
                    if not (9 * 60 + 30 <= bucket <= 15 * 60 + 59):
                        continue
                else:
                    continue
                close = bar.get("close")
                if isinstance(close, (int, float)) and close > 0:
                    last_close = float(close)
    except OSError:
        return None
    return last_close


def _prior_close_from_bar_archive(
    bar_archive_root: str,
    decision_date: str,
    *,
    max_lookback: int = 10,
) -> Optional[tuple[str, float]]:
    """Walk backward from `decision_date` to find the most recent
    SPY.jsonl with a valid RTH last-close. Returns (date_iso, close)
    or None if no archive file is found within `max_lookback` days.
    """
    try:
        dt = datetime.strptime(decision_date, "%Y-%m-%d")
    except ValueError:
        return None
    root = Path(bar_archive_root)
    for offset in range(1, max_lookback + 1):
        cand = (dt - timedelta(days=offset)).strftime("%Y-%m-%d")
        close = _last_rth_close_from_jsonl(root / cand / "SPY.jsonl")
        if close is not None:
            return (cand, close)
    return None


def _load_spy_csv_closes(csv_path: str) -> dict[str, float]:
    """Mirror of orb_vix_loader.load_vix_closes for SPY.
    Returns {YYYY-MM-DD: close}. Accepts datahub MM/DD/YYYY format and
    yfinance-style YYYY-MM-DD.
    """
    path = Path(csv_path)
    if not path.is_file():
        return {}
    out: dict[str, float] = {}
    try:
        with path.open() as fh:
            for row in csv.DictReader(fh):
                d = row.get("DATE") or row.get("Date") or row.get("date")
                c = row.get("CLOSE") or row.get("Close") or row.get("close")
                if not d or not c:
                    continue
                try:
                    try:
                        iso = datetime.strptime(d, "%m/%d/%Y").strftime("%Y-%m-%d")
                    except ValueError:
                        iso = datetime.strptime(d, "%Y-%m-%d").strftime("%Y-%m-%d")
                    out[iso] = float(c)
                except (ValueError, TypeError):
                    continue
    except OSError:
        return {}
    return out


def _walk_back_two_closes(
    closes: dict[str, float],
    decision_date: str,
    *,
    max_lookback: int = 10,
) -> Optional[tuple[str, float, str, float]]:
    """Find the two most recent CSV closes strictly before `decision_date`.
    Returns (d1_date, d1_close, d2_date, d2_close) where d1 is more recent
    than d2. Returns None if fewer than 2 prior closes available.
    """
    try:
        dt = datetime.strptime(decision_date, "%Y-%m-%d")
    except ValueError:
        return None
    found: list[tuple[str, float]] = []
    for offset in range(1, max_lookback + 1):
        cand = (dt - timedelta(days=offset)).strftime("%Y-%m-%d")
        if cand in closes:
            found.append((cand, closes[cand]))
            if len(found) == 2:
                d1_date, d1_close = found[0]
                d2_date, d2_close = found[1]
                return (d1_date, d1_close, d2_date, d2_close)
    return None


def prior_spy_return_bps(
    decision_date: str,
    *,
    bar_archive_root: str = DEFAULT_BAR_ARCHIVE_ROOT,
    csv_path: str = DEFAULT_CSV_PATH,
    max_lookback: int = 10,
) -> Optional[float]:
    """Return prior-session SPY close-to-close return in bps.

    Computed as (SPY[D-1] - SPY[D-2]) / SPY[D-2] * 10000 where D-1 and
    D-2 are the two most recent trading days strictly before
    `decision_date`. Returns None when fewer than 2 prior closes are
    available from either source.

    Source priority: bar archive first (live), then CSV (backtest).
    """
    # Try bar archive first.
    d1 = _prior_close_from_bar_archive(
        bar_archive_root,
        decision_date,
        max_lookback=max_lookback,
    )
    if d1 is not None:
        d1_date, d1_close = d1
        # Walk back from d1 to find d2.
        d2 = _prior_close_from_bar_archive(
            bar_archive_root,
            d1_date,
            max_lookback=max_lookback,
        )
        if d2 is not None:
            d2_date, d2_close = d2
            if d2_close > 0:
                bps = (d1_close - d2_close) / d2_close * 10000.0
                logger.debug(
                    "[V900-SPY-LOADER] bar archive: D-1 %s close=%.2f, "
                    "D-2 %s close=%.2f -> %.1fbps",
                    d1_date,
                    d1_close,
                    d2_date,
                    d2_close,
                    bps,
                )
                return bps
    # Fallback to CSV.
    closes = _load_spy_csv_closes(csv_path)
    if closes:
        pair = _walk_back_two_closes(closes, decision_date, max_lookback=max_lookback)
        if pair is not None:
            d1_date, d1_close, d2_date, d2_close = pair
            if d2_close > 0:
                bps = (d1_close - d2_close) / d2_close * 10000.0
                logger.debug(
                    "[V900-SPY-LOADER] CSV fallback: D-1 %s close=%.2f, "
                    "D-2 %s close=%.2f -> %.1fbps",
                    d1_date,
                    d1_close,
                    d2_date,
                    d2_close,
                    bps,
                )
                return bps
    logger.warning(
        "[V900-SPY-LOADER] no prior SPY close found for %s in either bar archive (%s) or CSV (%s)",
        decision_date,
        bar_archive_root,
        csv_path,
    )
    return None
