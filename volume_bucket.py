"""v5.10.0 \u2014 Institutional Oomph (Volume Bucket) baseline.

Section II.1 of Project Eye of the Tiger. The Entry-1 gate requires
1m volume >= 100% of the rolling 55-trading-day average for that
minute-of-day (HH:MM ET, RTH only).

Data source: /data/bars/<YYYY-MM-DD>/<TICKER>.jsonl archive that
v5.5.x writes via bar_archive.py. Each line is one 1m bar with at
least an `et_bucket` (HHMM string) and `iex_volume` (int).

Cold-start branch: if a ticker has < 55 trading days of bar history,
the gate is PASS-THROUGH with a once-per-session warning log
(VOLUME_BUCKET_COLD_START_PASSTHROUGH = True). Without pass-through,
virtually no entries would fire until the archive accumulates ~55
sessions, which would defeat Unlimited Hunting.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("trade_genius.volume_bucket")

VOLUME_BUCKET_LOOKBACK_DAYS = 55
VOLUME_BUCKET_THRESHOLD_RATIO = 1.00
VOLUME_BUCKET_COLD_START_PASSTHROUGH = True
VOLUME_BUCKET_REFRESH_HHMM_ET = "09:29"

DEFAULT_BARS_DIR = "/data/bars"


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def _trading_days_back(end: date, n: int) -> list[date]:
    """Return the most recent `n` trading days strictly before `end`,
    skipping weekends. Holidays are not modelled separately \u2014 the
    archive simply has no file for that date so it gets skipped at
    read time.
    """
    out: list[date] = []
    d = end - timedelta(days=1)
    while len(out) < n and (end - d).days < 365:
        if not _is_weekend(d):
            out.append(d)
        d = d - timedelta(days=1)
    return out


def _read_bars_for_day(base_dir: str, day: date, ticker: str) -> Iterable[dict]:
    """Yield bar dicts for a single (day, ticker). Missing file or
    parse errors yield nothing.
    """
    fp = Path(base_dir) / day.strftime("%Y-%m-%d") / f"{ticker.upper()}.jsonl"
    if not fp.exists():
        return
    try:
        with open(fp, "r", encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    yield json.loads(ln)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _bucket_key(et_bucket: str | int | None) -> str | None:
    """Normalise a bar's `et_bucket` to canonical 'HH:MM' (RTH only).

    Accepts:
      - 'HHMM' (e.g. '0930') \u2014 the format bar_archive writes
      - 'HH:MM'              \u2014 idempotent
      - int 930              \u2014 backwards-compat
    Rejects pre/post-market buckets (returns None).
    """
    if et_bucket is None:
        return None
    s = str(et_bucket).strip()
    if ":" in s:
        s = s.replace(":", "")
    if not s.isdigit():
        return None
    s = s.zfill(4)
    if len(s) != 4:
        return None
    hh = int(s[:2])
    mm = int(s[2:])
    if hh < 9 or hh > 15:
        return None
    if hh == 9 and mm < 30:
        return None
    if hh == 15 and mm > 59:
        return None
    return f"{hh:02d}:{mm:02d}"


class VolumeBucketBaseline:
    """Per-ticker, per-minute-of-day rolling 55-day volume baseline.

    Usage:
        bb = VolumeBucketBaseline(base_dir="/data/bars")
        bb.refresh(today=date.today())          # called at startup + 9:29 ET
        bb.check("AAPL", "09:35", 12500)        # -> {gate: PASS|FAIL|COLDSTART, ...}
    """

    def __init__(self, base_dir: str = DEFAULT_BARS_DIR,
                 lookback_days: int = VOLUME_BUCKET_LOOKBACK_DAYS,
                 threshold_ratio: float = VOLUME_BUCKET_THRESHOLD_RATIO,
                 cold_start_passthrough: bool = VOLUME_BUCKET_COLD_START_PASSTHROUGH):
        self.base_dir = base_dir
        self.lookback_days = lookback_days
        self.threshold_ratio = threshold_ratio
        self.cold_start_passthrough = cold_start_passthrough
        # baseline[ticker][minute_of_day_HHMM] = mean_volume
        self.baseline: dict[str, dict[str, float]] = {}
        self.days_available_per_ticker: dict[str, int] = {}
        self._cold_start_logged: set[str] = set()
        self.last_refresh_utc: datetime | None = None

    def refresh(self, today: date | None = None) -> None:
        """Recompute per-(ticker, minute) means over the last
        `lookback_days` trading days strictly before `today`.
        """
        if today is None:
            today = datetime.utcnow().date()
        days = _trading_days_back(today, self.lookback_days)

        sums: dict[str, dict[str, float]] = {}
        counts: dict[str, dict[str, int]] = {}
        days_seen: dict[str, set[date]] = {}

        for d in days:
            day_dir = Path(self.base_dir) / d.strftime("%Y-%m-%d")
            if not day_dir.exists() or not day_dir.is_dir():
                continue
            try:
                for fp in day_dir.iterdir():
                    if not fp.is_file() or not fp.name.endswith(".jsonl"):
                        continue
                    ticker = fp.stem.upper()
                    saw_any = False
                    for bar in _read_bars_for_day(self.base_dir, d, ticker):
                        key = _bucket_key(bar.get("et_bucket"))
                        if key is None:
                            continue
                        v = bar.get("iex_volume")
                        if v is None:
                            continue
                        try:
                            vf = float(v)
                        except (TypeError, ValueError):
                            continue
                        if vf < 0.0:
                            continue
                        sums.setdefault(ticker, {}).setdefault(key, 0.0)
                        counts.setdefault(ticker, {}).setdefault(key, 0)
                        sums[ticker][key] += vf
                        counts[ticker][key] += 1
                        saw_any = True
                    if saw_any:
                        days_seen.setdefault(ticker, set()).add(d)
            except OSError:
                continue

        new_baseline: dict[str, dict[str, float]] = {}
        for ticker, by_min in sums.items():
            new_baseline[ticker] = {}
            for k, total in by_min.items():
                n = counts[ticker].get(k, 0)
                if n > 0:
                    new_baseline[ticker][k] = total / n

        self.baseline = new_baseline
        self.days_available_per_ticker = {
            t: len(s) for t, s in days_seen.items()
        }
        self._cold_start_logged.clear()
        self.last_refresh_utc = datetime.utcnow()
        logger.info(
            "[V5100-VOLBUCKET-REFRESH] tickers=%d days_window=%d at=%s",
            len(new_baseline), self.lookback_days,
            self.last_refresh_utc.isoformat() + "Z",
        )

    def days_available(self, ticker: str) -> int:
        return self.days_available_per_ticker.get(ticker.upper(), 0)

    def check(self, ticker: str, minute_of_day: str,
              current_volume: float | int) -> dict:
        """Evaluate the gate. Returns dict with:
            gate: 'PASS' | 'FAIL' | 'COLDSTART'
            ratio: current_volume / baseline (or None if COLDSTART)
            baseline: float | None
            days_available: int
        """
        sym = ticker.upper()
        key = _bucket_key(minute_of_day)
        days = self.days_available(sym)
        if days < self.lookback_days:
            if self.cold_start_passthrough:
                if sym not in self._cold_start_logged:
                    logger.warning(
                        "[V5100-VOLBUCKET-COLDSTART] ticker=%s days_available=%d",
                        sym, days,
                    )
                    self._cold_start_logged.add(sym)
                return {"gate": "COLDSTART", "ratio": None,
                        "baseline": None, "days_available": days}
            return {"gate": "FAIL", "ratio": None,
                    "baseline": None, "days_available": days}
        if key is None:
            return {"gate": "FAIL", "ratio": None,
                    "baseline": None, "days_available": days}
        b = self.baseline.get(sym, {}).get(key)
        if b is None or b <= 0.0:
            return {"gate": "FAIL", "ratio": None,
                    "baseline": b, "days_available": days}
        try:
            cv = float(current_volume)
        except (TypeError, ValueError):
            return {"gate": "FAIL", "ratio": None,
                    "baseline": b, "days_available": days}
        ratio = cv / b
        gate = "PASS" if ratio >= self.threshold_ratio else "FAIL"
        return {"gate": gate, "ratio": ratio,
                "baseline": b, "days_available": days}
