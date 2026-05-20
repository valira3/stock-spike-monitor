"""Per-(date, ticker) premarket-feature cache.

Computing scan features (gap, pm dollar volume, pm range, premarket bar
count, prior close, NR-N range) for 504 tickers × 343 days takes ~3-4
min per call when reading JSONL on demand. The features are
deterministic for a given corpus snapshot \u2014 pre-compute once and pickle.

Cache file: data_pm_universe/.feature_cache.pkl
Schema v2: dict[(date_str, ticker)] -> tuple(gap_pct, pm_dollar_vol,
                                              pm_range_pct, n_pm_bars,
                                              prior_close,
                                              last_n_range_pct,
                                              baseline_14d_range_pct)

`last_n_range_pct`: (max_high − min_low) / mean_close over the last 5
premarket bars within the last 30 min before 09:30 ET. The "today"
side of the NR-N compression signal.

`baseline_14d_range_pct`: mean of `last_n_range_pct` over the prior
14 trading days for this ticker (None if fewer than 7 prior days
have a value). The "typical" side, used by the `compression_rel`
signal in premarket_scanner.

Build with `tools/build_scanner_cache.py`; consume via load_cache().
"""
from __future__ import annotations

import json
import pickle
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from orb.premarket_scanner import (
    RTH_OPEN_BUCKET,
    _load_bars,
    _premarket_bars,
)


# NR-N window: last 5 premarket bars within the last 30 min before 09:30 ET.
NR_N_LOOKBACK_BARS = 5
NR_N_WINDOW_MINUTES = 30
BASELINE_LOOKBACK_DAYS = 14
BASELINE_MIN_OBS = 7


@dataclass(frozen=True)
class FeatureRow:
    gap_pct: float           # signed (premarket-close − prior-rth-close) / prior-rth-close
    pm_dollar_vol: float     # sum of close*volume over premarket window
    pm_range_pct: float      # (premarket_high − premarket_low) / premarket_open
    n_pm_bars: int           # count of premarket bars
    prior_close: float       # last RTH bar's close from previous trading day


CACHE_FILENAME = ".feature_cache.pkl"


def _last_n_range_pct(bars: list[dict]) -> Optional[float]:
    """Compute (max_high - min_low) / mean_close over the last
    NR_N_LOOKBACK_BARS premarket bars within the last NR_N_WINDOW_MINUTES
    minutes before 09:30 ET. Returns None if insufficient bars."""
    rth_open_min = 9 * 60 + 30
    window_start = rth_open_min - NR_N_WINDOW_MINUTES
    in_window: list[tuple[int, dict]] = []
    for b in bars:
        bkt = b.get("et_bucket", "9999")
        try:
            mins = int(bkt[:2]) * 60 + int(bkt[2:])
        except (ValueError, IndexError):
            continue
        if window_start <= mins < rth_open_min:
            in_window.append((mins, b))
    if len(in_window) < NR_N_LOOKBACK_BARS:
        return None
    in_window.sort(key=lambda x: x[0])
    last_n = [b for _, b in in_window[-NR_N_LOOKBACK_BARS:]]
    highs = [float(b["high"]) for b in last_n]
    lows = [float(b["low"]) for b in last_n]
    closes = [float(b["close"]) for b in last_n]
    mc = sum(closes) / len(closes) if closes else 0.0
    if mc <= 0:
        return None
    return (max(highs) - min(lows)) / mc


def _compute_one_ticker(args: tuple[str, str, str]) -> tuple[tuple[str, str], Optional[tuple]]:
    """Worker entry point. Returns ((date, ticker), feature_tuple) or
    ((date, ticker), None) if insufficient data."""
    corpus_root, date_str, ticker = args
    corpus = Path(corpus_root)
    bars = _load_bars(corpus / date_str / f"{ticker}.jsonl")
    if not bars:
        return (date_str, ticker), None
    pm = _premarket_bars(bars)
    if not pm:
        return (date_str, ticker), None

    pm_open = float(pm[0]["open"])
    pm_close = float(pm[-1]["close"])
    pm_high = max(float(b["high"]) for b in pm)
    pm_low = min(float(b["low"]) for b in pm)
    pm_dol = sum(float(b["close"]) * float(b["total_volume"]) for b in pm)

    # Prior RTH close: walk back up to 7 calendar days
    from datetime import date as _d, timedelta
    cur = _d.fromisoformat(date_str)
    prior_close: Optional[float] = None
    for back in range(1, 8):
        prev = cur - timedelta(days=back)
        prev_bars = _load_bars(corpus / prev.isoformat() / f"{ticker}.jsonl")
        rth = [b for b in prev_bars
               if RTH_OPEN_BUCKET <= b.get("et_bucket", "9999") < "1600"]
        if rth:
            prior_close = float(rth[-1]["close"])
            break
    if prior_close is None or prior_close <= 0:
        return (date_str, ticker), None

    gap_pct = (pm_close - prior_close) / prior_close
    pm_range_pct = (pm_high - pm_low) / pm_open if pm_open > 0 else 0.0
    last_n_rng = _last_n_range_pct(bars)
    # baseline_14d_range_pct filled in in a second pass after the
    # parallel build, since it depends on prior-days' last_n_range_pct.
    return (date_str, ticker), (
        gap_pct, pm_dol, pm_range_pct, len(pm), prior_close,
        last_n_rng if last_n_rng is not None else 0.0,
        0.0,   # placeholder for baseline; filled in by _fill_baselines
    )


def _fill_baselines(out: dict[tuple[str, str], tuple]) -> None:
    """Second pass: for each (date, ticker), set baseline_14d_range_pct
    to the mean of the prior BASELINE_LOOKBACK_DAYS days' last_n_range_pct
    for that ticker. Requires at least BASELINE_MIN_OBS observations."""
    # Group rows by ticker, sorted by date
    from collections import defaultdict
    by_ticker: dict[str, list[tuple[str, list]]] = defaultdict(list)
    for (date_str, tk), row in out.items():
        by_ticker[tk].append((date_str, list(row)))
    # For each ticker, sort by date; walk forward and fill baseline
    for tk, rows in by_ticker.items():
        rows.sort(key=lambda r: r[0])
        # Sliding window of the last 14 days' last_n_range_pct (index 5 in row)
        from collections import deque
        window = deque(maxlen=BASELINE_LOOKBACK_DAYS)
        for date_str, row in rows:
            # Baseline = mean over the prior window (NOT including today)
            obs = [v for v in window if v > 0]
            if len(obs) >= BASELINE_MIN_OBS:
                row[6] = sum(obs) / len(obs)
            else:
                row[6] = 0.0
            # Append today's last_n_range_pct AFTER computing the baseline
            window.append(row[5])
            out[(date_str, tk)] = tuple(row)


def build_cache(
    corpus_root: Path | str,
    dates: list[str],
    tickers: list[str],
    workers: int = 8,
    progress_every: int = 50,
) -> dict[tuple[str, str], tuple]:
    """Compute features for every (date, ticker) in parallel, then
    fill per-ticker rolling baselines in a second pass."""
    corpus_root = str(corpus_root)
    tasks = [(corpus_root, d, t) for d in dates for t in tickers]
    n = len(tasks)
    print(f"Building feature cache: {len(dates)} days × {len(tickers)} tickers "
          f"= {n:,} (date,ticker) pairs, {workers} workers", flush=True)

    out: dict[tuple[str, str], tuple] = {}
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for key, feats in pool.map(_compute_one_ticker, tasks, chunksize=200):
            if feats is not None:
                out[key] = feats
            done += 1
            if done % progress_every == 0 or done == n:
                pct = 100 * done / n
                print(f"  {done:>7,}/{n:,}  ({pct:>5.1f}%)  rows kept: {len(out):,}",
                      flush=True)

    print("Filling 14d per-ticker baselines...", flush=True)
    _fill_baselines(out)
    print("Done.", flush=True)
    return out


def save_cache(cache: dict, corpus_root: Path | str) -> Path:
    p = Path(corpus_root) / CACHE_FILENAME
    with p.open("wb") as f:
        pickle.dump(cache, f, protocol=pickle.HIGHEST_PROTOCOL)
    return p


def load_cache(corpus_root: Path | str) -> Optional[dict]:
    p = Path(corpus_root) / CACHE_FILENAME
    if not p.is_file():
        return None
    with p.open("rb") as f:
        return pickle.load(f)
