"""Per-(date, ticker) premarket-feature cache.

Computing scan features (gap, pm dollar volume, pm range, premarket bar
count, prior close) for 504 tickers × 343 days takes ~3-4 min per call
when reading JSONL on demand. The features are deterministic for a
given corpus snapshot — pre-compute once and pickle.

Cache file: data_pm_universe/.feature_cache.pkl
Schema: dict[(date_str, ticker)] -> tuple(gap_pct, pm_dollar_vol,
                                          pm_range_pct, n_pm_bars,
                                          prior_close)

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


@dataclass(frozen=True)
class FeatureRow:
    gap_pct: float           # signed (premarket-close − prior-rth-close) / prior-rth-close
    pm_dollar_vol: float     # sum of close*volume over premarket window
    pm_range_pct: float      # (premarket_high − premarket_low) / premarket_open
    n_pm_bars: int           # count of premarket bars
    prior_close: float       # last RTH bar's close from previous trading day


CACHE_FILENAME = ".feature_cache.pkl"


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
    return (date_str, ticker), (gap_pct, pm_dol, pm_range_pct, len(pm), prior_close)


def build_cache(
    corpus_root: Path | str,
    dates: list[str],
    tickers: list[str],
    workers: int = 8,
    progress_every: int = 50,
) -> dict[tuple[str, str], tuple[float, float, float, int, float]]:
    """Compute features for every (date, ticker) in parallel."""
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
