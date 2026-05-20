"""Pre-pickle per-day bar caches for the simulator.

Mirrors the trick orb_backtest.py uses (`data/.bt_cache/<TICKER>.pkl`)
but at the *per-day* granularity that the simulator needs. The
simulator iterates day-by-day in spawn workers, and each worker's
BarFeeder.from_corpus call enumerates ~500 ticker JSONL files +
parses ~20k JSON lines. The hot path is per-line `json.loads` plus
500 file opens, which is ~100ms per day -> ~30s of cumulative I/O
across a full year per worker even after parallelism.

This script pre-builds a single pickle per date holding the entire
day's bars indexed by upper-cased ticker:

    <corpus_root>/.bt_cache/<YYYY-MM-DD>.pkl
        -> {ticker_upper: [bar_dict, bar_dict, ...]}

BarFeeder.from_corpus auto-detects the pickle and loads it when
present; otherwise falls back to the JSONL path. The pickle is
invalidated when any of the day's JSONL files is newer than the
pickle (mtime check).

Usage:
    python tools/build_sim_bar_cache.py                          # all dates
    python tools/build_sim_bar_cache.py --corpus data_pm_universe
    python tools/build_sim_bar_cache.py --from 2025-07-01 --to 2025-07-31
    python tools/build_sim_bar_cache.py --rebuild                # force-rebuild
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
from multiprocessing import Pool, cpu_count


CACHE_DIRNAME = ".bt_cache"


def build_one_day(args) -> tuple[str, bool, str]:
    """Worker: build per-ticker pickles for a single date.

    Layout: <corpus_root>/.bt_cache/<date>/<TICKER>.pkl
    Each pickle is a list[dict] (the day's bars for that ticker).

    Per-ticker (not per-day) so the BarFeeder loads only the small
    pickles for the requested universe instead of deserializing the
    full ~500-ticker day in one shot.

    Returns (date, ok, msg).
    """
    date, corpus_root, rebuild = args
    day_dir = os.path.join(corpus_root, date)
    if not os.path.isdir(day_dir):
        return (date, False, "no day dir")
    cache_dir = os.path.join(corpus_root, CACHE_DIRNAME, date)
    os.makedirs(cache_dir, exist_ok=True)

    built = 0
    skipped = 0
    n_bars = 0

    for fname in os.listdir(day_dir):
        if not fname.endswith(".jsonl"):
            continue
        ticker = fname[: -len(".jsonl")].upper()
        src_path = os.path.join(day_dir, fname)
        pkl_path = os.path.join(cache_dir, f"{ticker}.pkl")

        # Staleness check: per-ticker pkl vs its own JSONL.
        if not rebuild and os.path.isfile(pkl_path):
            try:
                if os.path.getmtime(pkl_path) >= os.path.getmtime(src_path):
                    skipped += 1
                    continue
            except OSError:
                pass

        rows: list[dict] = []
        with open(src_path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        if not rows:
            continue
        n_bars += len(rows)

        tmp_path = pkl_path + ".tmp"
        with open(tmp_path, "wb") as fh:
            pickle.dump(rows, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp_path, pkl_path)
        built += 1

    return (date, True, f"{built} built, {skipped} skipped, {n_bars} bars")


def list_dates(corpus_root: str, from_d: str | None, to_d: str | None) -> list[str]:
    if not os.path.isdir(corpus_root):
        return []
    out = []
    for name in sorted(os.listdir(corpus_root)):
        if len(name) != 10 or name[4] != "-" or name[7] != "-":
            continue
        if from_d and name < from_d:
            continue
        if to_d and name > to_d:
            continue
        out.append(name)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--corpus", default="data_pm_universe",
                   help="Corpus root (default: data_pm_universe)")
    p.add_argument("--from", dest="from_d",
                   help="Inclusive start date YYYY-MM-DD")
    p.add_argument("--to", dest="to_d",
                   help="Inclusive end date YYYY-MM-DD")
    p.add_argument("--rebuild", action="store_true",
                   help="Force rebuild even if pkl is fresh")
    p.add_argument("--workers", type=int, default=0,
                   help="Worker count (0 = cpu_count/2)")
    args = p.parse_args(argv)

    dates = list_dates(args.corpus, args.from_d, args.to_d)
    if not dates:
        print(f"No dates found in {args.corpus}")
        return 1

    workers = args.workers or max(1, cpu_count() // 2)
    print(f"[bar-cache] building {len(dates)} dates with {workers} workers...")
    t0 = time.time()

    work = [(d, args.corpus, args.rebuild) for d in dates]

    if workers == 1:
        results = [build_one_day(w) for w in work]
    else:
        with Pool(processes=workers) as pool:
            results = list(pool.imap(build_one_day, work, chunksize=4))

    ok = sum(1 for r in results if r[1])
    fresh = sum(1 for r in results if r[1] and "skipped" in r[2])
    rebuilt = ok - fresh
    failed = [r for r in results if not r[1]]
    elapsed = time.time() - t0
    print(f"[bar-cache] done in {elapsed:.1f}s: {rebuilt} rebuilt, "
          f"{fresh} fresh, {len(failed)} failed")
    for r in failed[:5]:
        print(f"  {r[0]}: {r[2]}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
